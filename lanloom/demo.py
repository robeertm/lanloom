"""Demo mode (LANLOOM_DEMO=1): a fictional home network with synthetic live data.

Nothing is scanned and nothing is contacted. Every name, address and value here is invented; the scan in the
setup wizard is simulated and the discovered hosts still go through the real classifier. Useful to try the
product and for documentation screenshots.
"""
import asyncio
import json
import math
import random
import time

from . import demo_paths_detail

NET = "192.168.50.0/24"


def _mac(n: int) -> str:
    return "02:4c:4c:00:00:%02x" % n          # locally administered, not a real vendor prefix


def _vmac(n: int) -> str:
    return "52:54:00:4c:00:%02x" % n          # the address block hypervisors hand to virtual network cards


def _port(port, service, title=None, server=None):
    banner = {"title": title, "server": server} if (title or server) else None
    return {"port": port, "proto": "tcp", "service": service, "banner": banner}


HOSTS = [
    {"ip": "192.168.50.1", "n": 1, "gw": True, "names": ["gateway.lan"],
     "ports": [_port(53, "dns"), _port(80, "http", "Router"), _port(443, "https", "Router")],
     "ssdp": [{"st": "urn:schemas-upnp-org:device:InternetGatewayDevice:1", "friendly_name": "Router", "model": "Home gateway HG-1",
               "manufacturer": "Example Networks", "device_type": "urn:schemas-upnp-org:device:InternetGatewayDevice:1", "server": None}]},
    {"ip": "192.168.50.2", "n": 2, "names": ["core-switch.lan"],
     "ports": [_port(80, "http", "Switch management"), _port(443, "https", "Switch management")],
     "snmp": {"descr": "Managed switch SW-8, firmware 2.4.1", "object_id": "1.3.6.1.4.1.32473.1.8", "name": "core-switch",
              "enterprise": 32473}},
    {"ip": "192.168.50.3", "n": 3, "names": ["ap-upstairs.lan"],
     "ports": [_port(443, "https", "Access point")],
     "ssdp": [{"st": "urn:schemas-upnp-org:device:WLANAccessPointDevice:1", "friendly_name": "Access point upstairs",  # identifier: UPnP device type
               "model": None, "manufacturer": None, "device_type": None, "server": None}]},
    {"ip": "192.168.50.4", "n": 4, "names": ["ap-garden.lan"],
     "ports": [_port(443, "https", "Access point")],
     "ssdp": [{"st": "urn:schemas-upnp-org:device:WLANAccessPointDevice:1", "friendly_name": "Access point garden",  # identifier: UPnP device type
               "model": None, "manufacturer": None, "device_type": None, "server": None}]},
    {"ip": "192.168.50.10", "n": 10, "names": ["storage.local"],
     "ports": [_port(22, "ssh"), _port(80, "http"), _port(443, "https"), _port(445, "smb"), _port(5000, "web-alt", "Storage"),
               _port(5001, "web-alt-tls", "Storage")],
     "mdns": [{"type": "_smb._tcp", "name": "Storage", "port": 445, "txt": {}}, {"type": "_nfs._tcp", "name": "Storage", "port": 2049, "txt": {}},
              {"type": "_device-info._tcp", "name": "Storage", "port": 0, "txt": {"model": "ST-4 Bay", "fw": "7.2.1"}}],
     "ssdp": [{"st": "upnp:rootdevice", "friendly_name": "Storage", "model": "ST-4 Bay", "manufacturer": None,
               "device_type": "urn:schemas-upnp-org:device:Basic:1", "server": None, "udn": "4c4c0000-0000-4000-8000-00000000000a"}],
     "ipv6": ["fd4c:4c00:50::10", "fe80::4c:4cff:fe00:a"]},
    {"ip": "192.168.50.16", "n": 16, "names": ["storage.local"], "second_nic": True,
     "ports": [_port(445, "smb"), _port(5001, "web-alt-tls", "Storage")],
     "ssdp": [{"st": "upnp:rootdevice", "friendly_name": "Storage", "model": "ST-4 Bay", "manufacturer": None,
               "device_type": "urn:schemas-upnp-org:device:Basic:1", "server": None, "udn": "4c4c0000-0000-4000-8000-00000000000a"}]},
    {"ip": "192.168.50.11", "n": 11, "self": True, "names": ["homeserver.lan"],
     "ports": [_port(22, "ssh"), _port(80, "http"), _port(443, "https"), _port(8080, "http-alt"), _port(8443, "https-alt", "Network controller")]},
    {"ip": "192.168.50.12", "n": 12, "names": ["backup.lan"],
     "ports": [_port(22, "ssh"), _port(873, "rsync")], "title": "Backup server"},
    {"ip": "192.168.50.13", "n": 13, "vm": True, "names": ["broker.lan"],
     "ports": [_port(1883, "mqtt"), _port(8883, "mqtts")],
     "mdns": [{"type": "_mqtt._tcp", "name": "Message broker", "port": 1883, "txt": {}}]},
    {"ip": "192.168.50.14", "n": 14, "vm": True, "names": ["automation.local"],
     "ports": [_port(8123, "web-alt", "Automation")],
     "mdns": [{"type": "_home-assistant._tcp", "name": "Automation hub", "port": 8123, "txt": {}}]},  # identifier: demo DNS-SD type
    {"ip": "192.168.50.15", "n": 15, "names": ["virtualization-host.lan"],
     "ports": [_port(22, "ssh"), _port(443, "https", "Virtualization host"), _port(8006, "hypervisor-web", "Virtualization host")],
     "title": "Virtualization host"},
    {"ip": "192.168.50.20", "n": 20, "names": ["office-laptop.lan"],
     "ports": [_port(22, "ssh")],
     "mdns": [{"type": "_workstation._tcp", "name": "Office laptop", "port": 9, "txt": {}}],
     "ipv6": ["fd4c:4c00:50::20", "fe80::4c:4cff:fe00:14"]},
    {"ip": "192.168.50.21", "n": 21, "mac": "00:00:5e:00:53:44", "names": ["studio-desktop.lan"],
     "ports": [_port(139, "netbios"), _port(445, "smb"), _port(3389, "remote-desktop")], "title": "Studio desktop",
     "netbios": {"computer": "STUDIO-PC", "workgroup": "HOME"},
     "wsd": [{"types": ["wsdp:Device", "pub:Computer"], "xaddrs": [], "endpoint": None}]},
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
     "mdns": [{"type": "_ipp._tcp", "name": "Office printer", "port": 631, "txt": {"ty": "Office Laser P-200", "usb_MFG": "Example Imaging"}}],
     "wsd": [{"types": ["wsdp:Device", "wprt:PrintDeviceType"], "xaddrs": [], "endpoint": None}]},
    {"ip": "192.168.50.50", "n": 50, "names": ["front-door-cam.lan"],
     "ports": [_port(80, "http", "Camera"), _port(554, "rtsp")], "title": "Front door camera",
     "wsd": [{"types": ["dn:NetworkVideoTransmitter"], "xaddrs": [], "endpoint": None}]},
    {"ip": "192.168.50.51", "n": 51, "names": ["garage-cam.lan"],
     "ports": [_port(80, "http", "Camera"), _port(554, "rtsp")], "title": "Garage camera"},
    {"ip": "192.168.50.60", "n": 60, "names": ["plug-washer.lan"], "ports": [_port(80, "http", "Smart plug")], "title": "Washer plug"},
    {"ip": "192.168.50.61", "n": 61, "names": ["thermostat-hall.lan"], "ports": [_port(80, "http", "Thermostat")], "title": "Hall thermostat"},
    {"ip": "192.168.50.62", "n": 62, "names": ["energy-meter.lan"], "ports": [_port(80, "http", "Energy meter sensor")], "title": "Energy meter"},
    {"ip": "192.168.50.63", "n": 63, "names": ["light-strip.lan"], "ports": [_port(80, "http", "Light strip")], "title": "Desk light strip"},
    {"ip": "192.168.50.64", "n": 64, "names": ["air-quality.lan"], "ports": [_port(80, "http", "Air quality sensor")], "title": "Air quality sensor"},
    {"ip": "192.168.50.65", "n": 65, "names": ["vacuum.lan"], "ports": [_port(80, "http", "Robot vacuum")], "title": "Robot vacuum"},
    {"ip": "192.168.50.66", "n": 66, "names": ["inverter.lan"], "ports": [_port(80, "http", "Solar inverter sensor")], "title": "Solar inverter",
     "mac": "4c:4c:00:00:00:42"},
    {"ip": "192.168.50.67", "n": 67, "names": [], "ports": [_port(80, "http", "Inverter service")], "mac": "4c:4c:00:00:00:43"},
    {"ip": "192.168.50.99", "n": 99, "names": [], "ports": [_port(49152, "upnp")]},
]

AREAS = [("living_room", "Living room", 6, 31), ("kitchen", "Kitchen", 5, 24), ("office", "Office", 4, 19),
         ("bedroom", "Bedroom", 3, 12), ("garden", "Garden", 4, 15), ("garage", "Garage", 2, 9), ("hallway", "Hallway", 3, 11)]
TOPICS = [("sensors/climate", 6.0, 180), ("energy/meter", 4.0, 220), ("home/presence", 0.5, 90), ("garden/irrigation", 0.2, 140),
          ("solar/inverter", 1.0, 400), ("radio/bridge", 9.0, 160), ("lights/state", 1.5, 120)]


# Physical layout of the demo network: who hangs on which switch port or access point (host number -> link).
SWITCH, AP_1, AP_2, GATEWAY = 2, 3, 4, 1
WIRED = {AP_1: "2", 10: "3", 11: "4", 12: "5", 15: "6", 13: "6", 14: "6", 21: "7", 30: "8", 40: "10"}
RADIO = {20: (AP_1, "5ghz"), 22: (AP_1, "5ghz"), 31: (AP_1, "5ghz"), 32: (AP_1, "5ghz"), 60: (AP_1, "2.4ghz"),
         61: (AP_1, "2.4ghz"), 62: (AP_1, "2.4ghz"), 63: (AP_1, "2.4ghz"), 23: (AP_2, "5ghz"), 50: (AP_2, "2.4ghz"),
         51: (AP_2, "2.4ghz"), 64: (AP_2, "2.4ghz"), 65: (AP_2, "2.4ghz"), 66: (AP_2, "2.4ghz"), 67: (AP_2, "2.4ghz")}
MANUAL = {40: (SWITCH, "9")}          # the printer was re-plugged: the manual entry wins over the controller's older view
SSID = "Home"


def _host(n: int) -> dict:
    return next(h for h in HOSTS if h["n"] == n)


def _hmac(n: int) -> str:
    h = _host(n)
    return h.get("mac") or (_vmac(n) if h.get("vm") else _mac(n))


# Fictional address register of the demo (screenshots never show real organisation names): host number -> maker.
# Phones (22, 23) and the virtual machines (13, 14) use private addresses; host 99 is in no register.
MAKERS = {1: "Example Networks", 2: "Example Networks", 3: "Example Networks", 4: "Example Networks", 10: "Example Storage",
          16: "Example Storage", 11: "Example Systems", 12: "Example Systems", 15: "Example Systems", 20: "Example Computers",
          21: "Example Computers", 30: "Example Corp", 31: "Example Audio", 32: "Example Corp", 40: "Example Imaging",
          50: "Example Vision", 51: "Example Vision", 60: "Example Sensors", 61: "Example Sensors", 62: "Example Sensors",
          63: "Example Sensors", 64: "Example Sensors", 65: "Example Sensors", 66: "Example Energy", 67: "Example Energy"}
RANDOM_MACS = (22, 23, 13, 14)

# Devices only an adapter knows (the sample controller lists them; nothing was scanned there): networks behind the
# router in their own VLANs, plus two hardware addresses without an IP address.
# (mac, ip or None, name, medium, parent host number, port or band, maker or None)
ADAPTER_CLIENTS = (
    ("00:00:5e:00:60:10", "192.168.60.11", "Window sensor hall", "radio", AP_1, "2.4ghz", "Example Sensors"),
    ("00:00:5e:00:60:30", "192.168.60.12", "Window sensor kitchen", "radio", AP_1, "2.4ghz", "Example Sensors"),
    ("00:00:5e:00:60:50", "192.168.60.13", "Smart plug TV", "radio", AP_1, "2.4ghz", "Example Energy"),
    ("00:00:5e:00:60:70", "192.168.60.14", "Thermostat bedroom", "radio", AP_2, "2.4ghz", "Example Energy"),
    ("00:00:5e:00:70:10", "192.168.70.21", "Camera garage", "wired", SWITCH, "11", "Example Vision"),
    ("00:00:5e:00:70:30", "192.168.70.22", "Camera driveway", "wired", SWITCH, "12", "Example Vision"),
    ("da:a1:19:7e:41:c3", "192.168.80.31", "Guest phone", "radio", AP_1, "5ghz", None),
    # a second network card of the studio desktop (docking station): same maker block, same switch port, seen at the same
    # time, no IP address
    ("00:00:5e:00:53:9a", None, None, "wired", SWITCH, "7", None),
)
ADAPTER_FDB = (("00:00:5e:00:90:11", "lan2"),)        # an address the router's table learned, nothing else known


def registry():
    from .oui import Oui
    reg = Oui()
    names = {_hmac(n): name for n, name in MAKERS.items()}
    names.update({m: maker for m, _ip, _nm, _md, _par, _pt, maker in ADAPTER_CLIENTS if maker})
    reg.override = {"names": names, "random": {_hmac(n) for n in RANDOM_MACS} | {"da:a1:19:7e:41:c3"}}
    return reg


# Data paths (synthetic history): host number -> [(country, MB sent per day, MB received per day)]. "~own" is a tunnel,
# "~anycast" a well-known anycast network (content delivery, public resolvers, time service).
PATHS_HOME = "NL"
PATHS = {
    11: [("NL", 40, 300), ("US", 15, 120), ("DE", 5, 40), ("IE", 3, 20), ("~own", 30, 30)],
    10: [("NL", 300, 80), ("CH", 120, 10)],
    12: [("NL", 20, 4)],
    14: [("NL", 5, 20), ("US", 2, 10), ("FI", 1, 3)],
    20: [("NL", 80, 600), ("US", 60, 400), ("FR", 10, 80), ("SE", 5, 120), ("GB", 4, 30), ("~own", 12, 40), ("~anycast", 15, 450)],
    22: [("NL", 25, 300), ("US", 30, 350), ("IE", 6, 60), ("SG", 2, 20), ("~anycast", 4, 90)],
    23: [("US", 10, 400), ("NL", 8, 150)],
    30: [("US", 20, 2500), ("NL", 5, 900), ("IE", 2, 300)],
    31: [("US", 3, 60), ("IE", 1, 30)],
    32: [("US", 4, 700), ("GB", 1, 90)],
    51: [("CN", 12, 1)],
    65: [("CN", 2, 1), ("SG", 1, 0.5)],
    66: [("NL", 1, 0.3), ("DE", 0.5, 0.2), ("~anycast", 0.05, 0.05)],
}
# Conversations between two devices of the LAN (as a gateway would report them across networks): host number, host number,
# MB per day from the first to the second, MB per day back. The map's "Who talks to whom" draws them as dashed lines.
PATH_PAIRS = [
    (20, 10, 900, 60),     # laptop <-> storage: backup and file sharing
    (22, 10, 400, 30),     # desktop <-> storage
    (30, 10, 30, 1800),    # TV <- storage: media streaming
    (50, 10, 700, 2),      # camera -> storage (recordings)
    (51, 10, 650, 2),      # second camera -> storage
    (11, 14, 6, 4),        # hub <-> smart plug
    (11, 12, 3, 2),        # hub <-> printer status
    (20, 12, 8, 1),        # laptop -> printer
    (23, 11, 5, 3),        # phone <-> hub
]
PATHS_NO_INTERNET = ["192.168.50.50/31"]      # the two cameras should not reach the internet
PATHS_NEW = (30, "KR", 45)                    # the TV talks to a new country since a few hours (MB)
PATHS_HEAVY = (10, "NL", 1900)                # the storage uploads far more than usual today (MB in 24 h)
PATHS_EXPORTER = "192.168.50.1"               # the demo router as NetFlow exporter (the demo never opens the port)


def _diurnal(hour: int) -> float:
    h = hour % 24
    return 0.35 + 1.3 * math.exp(-((h - 20) % 24 - 0) ** 2 / 18.0) + 0.5 * math.exp(-((h - 12) ** 2) / 10.0)


def seed_paths(core, now: float = None, seed: int = 11) -> None:
    """Thirty days of synthetic data paths for the demo (only if there are none yet)."""
    from . import demo_routes
    demo_routes.seed(core, now)             # network routes of the sample network (synthetic, nothing is measured)
    if core.store.one("SELECT hour FROM paths_hour LIMIT 1") or core.store.one("SELECT day FROM paths_day LIMIT 1"):
        return
    rng = random.Random(seed)
    now = now or time.time()
    now_h = int(now // 3600)
    norm = sum(_diurnal(h) for h in range(24)) / 24.0
    hours, days, seen = {}, {}, {}
    ids = {int(ip.rsplit(".", 1)[1]): dev for ip, dev in core.ip_to_device.items() if ip.startswith("192.168.50.")}
    for n, entries in PATHS.items():
        dev = ids.get(n)
        if not dev:
            continue
        for cc, tx_mb, rx_mb in entries:
            first = now - 30 * 86400 + (20 * 86400 if n == 51 else 0)
            for h in range(now_h - 30 * 24, now_h + 1):
                ts = h * 3600
                if ts < first:
                    continue
                f = _diurnal(h) / norm / 24.0 * (0.6 + 0.8 * rng.random())
                tx, rx = int(tx_mb * 1e6 * f), int(rx_mb * 1e6 * f)
                if (n, cc) == PATHS_HEAVY[:2] and h > now_h - 24:
                    tx += int(PATHS_HEAVY[2] * 1e6 / 24)
                if h > now_h - 49:
                    hours[(h, dev, cc)] = [tx, rx]
                d = days.setdefault((h // 24, dev, cc), [0, 0])
                d[0] += tx; d[1] += rx
            if not cc.startswith("~"):
                seen[(dev, cc)] = first
    dev = ids.get(PATHS_NEW[0])
    if dev:
        for h in range(now_h - 4, now_h + 1):
            b = int(PATHS_NEW[2] * 1e6 / 5)
            hours[(h, dev, PATHS_NEW[1])] = [b // 8, b]
            d = days.setdefault((h // 24, dev, PATHS_NEW[1]), [0, 0])
            d[0] += b // 8; d[1] += b
        seen[(dev, PATHS_NEW[1])] = (now_h - 4) * 3600
    dpd = demo_paths_detail
    det_h, det_d = {}, {}
    for (when, dev, cc), (tx, rx) in hours.items():
        for key, asn, a, b in dpd.split(cc, tx, rx):
            e = det_h.setdefault((when, dev, cc, key, asn), [0, 0]); e[0] += a; e[1] += b
    for (when, dev, cc), (tx, rx) in days.items():
        for key, asn, a, b in dpd.split(cc, tx, rx):
            e = det_d.setdefault((when, dev, cc, key, asn), [0, 0]); e[0] += a; e[1] += b
    with core.store.transaction() as st:
        st._db.executemany("INSERT INTO paths_hour(hour, device, cc, tx, rx) VALUES(?,?,?,?,?)",
                           [(k[0], k[1], k[2], v[0], v[1]) for k, v in hours.items()])
        st._db.executemany("INSERT INTO paths_day(day, device, cc, tx, rx) VALUES(?,?,?,?,?)",
                           [(k[0], k[1], k[2], v[0], v[1]) for k, v in days.items()])
        st._db.executemany("INSERT INTO paths_city_hour(hour, device, cc, city, asn, tx, rx) VALUES(?,?,?,?,?,?,?)",
                           [(*k, *v) for k, v in det_h.items()])
        st._db.executemany("INSERT INTO paths_city_day(day, device, cc, city, asn, tx, rx) VALUES(?,?,?,?,?,?,?)",
                           [(*k, *v) for k, v in det_d.items()])
        st._db.executemany("INSERT OR REPLACE INTO paths_place(cc, city, name, region, lat, lon) VALUES(?,?,?,?,?,?)",
                           [row for cc in dpd.CITIES for row in dpd.places(cc)])
        st._db.executemany("INSERT OR REPLACE INTO paths_operator(asn, name) VALUES(?,?)", list(dpd.operators().items()))
        st._db.executemany("INSERT OR REPLACE INTO paths_seen(device, cc, first, last) VALUES(?,?,?,?)",
                           [(k[0], k[1], v, now) for k, v in seen.items()])
    core.geo.city.demo = True              # the sample network answers as if the city option were on (its figures are synthetic)
    core.store.set_setting("paths_detail", True)
    if not core.store.get_setting("home_country", ""):
        core.store.set_setting("home_country", PATHS_HOME)
    if not core.store.get_setting("paths_no_internet", []):
        core.store.set_setting("paths_no_internet", PATHS_NO_INTERNET)
    if core.store.get_setting("netflow_exporters", None) is None:      # the demo router "sends" NetFlow (nothing listens)
        core.store.set_setting("netflow_exporters", [PATHS_EXPORTER])
        core.store.set_setting("netflow_enabled", True)


def seed_pairs(core, now: float = None, seed: int = 13) -> None:
    """Thirty days of synthetic conversations between devices (only if there are none yet)."""
    if core.store.one("SELECT hour FROM paths_pair_hour LIMIT 1") or core.store.one("SELECT day FROM paths_pair_day LIMIT 1"):
        return
    rng = random.Random(seed)
    now = now or time.time()
    now_h = int(now // 3600)
    norm = sum(_diurnal(h) for h in range(24)) / 24.0
    ids = {int(ip.rsplit(".", 1)[1]): dev for ip, dev in core.ip_to_device.items() if ip.startswith("192.168.50.")}
    hours, days = {}, {}
    for n1, n2, ab_mb, ba_mb in PATH_PAIRS:
        d1, d2 = ids.get(n1), ids.get(n2)
        if not d1 or not d2:
            continue
        a, b = (d1, d2) if d1 < d2 else (d2, d1)
        mb_ab, mb_ba = (ab_mb, ba_mb) if d1 == a else (ba_mb, ab_mb)
        for h in range(now_h - 30 * 24, now_h + 1):
            f = _diurnal(h) / norm / 24.0 * (0.6 + 0.8 * rng.random())
            ab, ba = int(mb_ab * 1e6 * f), int(mb_ba * 1e6 * f)
            if h > now_h - 49:
                hours[(h, a, b)] = [ab, ba]
            d = days.setdefault((h // 24, a, b), [0, 0])
            d[0] += ab; d[1] += ba
    with core.store.transaction() as st:
        st._db.executemany("INSERT INTO paths_pair_hour(hour, a, b, ab, ba) VALUES(?,?,?,?,?)",
                           [(k[0], k[1], k[2], v[0], v[1]) for k, v in hours.items()])
        st._db.executemany("INSERT INTO paths_pair_day(day, a, b, ab, ba) VALUES(?,?,?,?,?)",
                           [(k[0], k[1], k[2], v[0], v[1]) for k, v in days.items()])


def seed_segplan(core) -> None:
    """A segmentation plan on the sample network of the firewall tab (target networks per device; Lanloom data)."""
    if core.store.get_setting("fw_segplan", None) is None:
        from .firewall import segplan
        from .firewall.adapter import SAMPLE_SEGPLAN
        segplan.set_targets(core.store, SAMPLE_SEGPLAN)


def book_paths_minute(core, now: float, rng) -> None:
    """One minute of synthetic data paths (the demo keeps growing while it runs)."""
    ids = {int(ip.rsplit(".", 1)[1]): dev for ip, dev in core.ip_to_device.items() if ip.startswith("192.168.50.")}
    norm = sum(_diurnal(h) for h in range(24)) / 24.0
    f = _diurnal(int(now // 3600)) / norm / 1440.0
    for n, entries in PATHS.items():
        dev = ids.get(n)
        if dev:
            for cc, tx_mb, rx_mb in entries:
                k = f * (0.6 + 0.8 * rng.random())
                tx, rx = int(tx_mb * 1e6 * k), int(rx_mb * 1e6 * k)
                core.paths.book_bucket(dev, cc, tx, rx, now)
                if not cc.startswith("~"):
                    for key, asn, a, b in demo_paths_detail.split(cc, tx, rx):
                        core.paths.book_detail_bucket(dev, cc, key, asn, a, b, now)
    for n1, n2, ab_mb, ba_mb in PATH_PAIRS:
        d1, d2 = ids.get(n1), ids.get(n2)
        if d1 and d2:
            k = f * (0.6 + 0.8 * rng.random())
            core.paths.book_pair(d1, d2, int(ab_mb * 1e6 * k), now)
            core.paths.book_pair(d2, d1, int(ba_mb * 1e6 * k), now)


def controller_event(now: float) -> dict:
    """What a switch / access point controller would report for the demo network (see sources/controller.py)."""
    devices = [
        {"mac": _mac(SWITCH), "ip": _host(SWITCH)["ip"], "name": "Core switch", "role": "switch", "model": "SW-8",
         "uplink": {"mac": _mac(GATEWAY), "port": None, "own_port": "1", "medium": "wired"}},
        {"mac": _mac(AP_1), "ip": _host(AP_1)["ip"], "name": "Access point upstairs", "role": "ap", "model": "AP-2",
         "uplink": {"mac": _mac(SWITCH), "port": "2", "own_port": "1", "medium": "wired"}},
        {"mac": _mac(AP_2), "ip": _host(AP_2)["ip"], "name": "Access point garden", "role": "ap", "model": "AP-2",
         "uplink": {"mac": _mac(AP_1), "port": None, "own_port": None, "medium": "radio"}, "online": False},   # the outage of the demo
    ]
    clients = []
    for n, port in WIRED.items():
        if n in (AP_1,):
            continue
        clients.append({"mac": _hmac(n), "ip": _host(n)["ip"], "name": None, "medium": "wired", "parent_mac": _mac(SWITCH),
                        "port": port, "band": None, "ssid": None})
    for n, (ap, band) in RADIO.items():
        if n in (20, 22) and int(now // 1500) % 2:        # two phones walk between the access points while the demo runs
            ap = AP_2 if ap == AP_1 else AP_1
        clients.append({"mac": _hmac(n), "ip": _host(n)["ip"], "name": None, "medium": "radio", "parent_mac": _mac(ap),
                        "port": None, "band": band, "ssid": SSID})
    for mac, ip, name, medium, parent, where, _maker in ADAPTER_CLIENTS:
        radio = medium == "radio"
        clients.append({"mac": mac, "ip": ip, "name": name, "medium": medium, "parent_mac": _mac(parent),
                        "port": None if radio else where, "band": where if radio else None, "ssid": "Things" if radio else None})
    equipment = [{"id": "e_ap1", "mac": _mac(AP_1), "ip": _host(AP_1)["ip"], "kind": "ap"},     # the sample controller's own ids (demo_live)
                 {"id": "e_ap2", "mac": _mac(AP_2), "ip": _host(AP_2)["ip"], "kind": "ap"}]
    return {"type": "topology", "source": "controller", "t": now, "devices": devices, "clients": clients, "equipment": equipment}


def router_topology_event(now: float) -> dict:
    """LLDP neighbours of the demo router over SNMP (see sources/snmp.py): the core switch on its LAN port."""
    return {"type": "topology", "source": "snmp", "host": _host(GATEWAY)["ip"], "t": now,
            "agent": {"ip": _host(GATEWAY)["ip"], "mac": _mac(GATEWAY), "name": "router"},
            "lldp": [{"local_port": "lan1", "local_port_num": 2, "chassis_mac": _mac(SWITCH), "chassis_ip": None, "chassis_text": None,
                      "port_id": "1", "port_mac": None, "port_desc": "Port 1", "sys_name": "core-switch", "caps": ["bridge"],
                      "mgmt_ip": _host(SWITCH)["ip"]}],
            "fdb": [{"mac": mac, "port": port} for mac, port in ADAPTER_FDB]}


def scan_host(h: dict) -> dict:
    title = h.get("title")
    ports = list(h.get("ports") or [])
    if title and ports:
        p0 = dict(ports[0])
        p0["banner"] = {"title": title, "server": None}
        ports[0] = p0
    extra = [k for k, src in (("ndp", "ipv6"), ("wsd", "wsd"), ("netbios", "netbios"), ("snmp", "snmp")) if h.get(src)]
    mac = h.get("mac") or (_vmac(h["n"]) if h.get("vm") else _mac(h["n"]))
    return {"ip": h["ip"], "mac": mac, "hostnames": list(h.get("names") or []),
            "sources": ["arp"] + (["mdns"] if h.get("mdns") else []) + (["ssdp"] if h.get("ssdp") else []) + (["tcp"] if ports else []) + extra,
            "ports": ports, "mdns": list(h.get("mdns") or []), "ssdp": list(h.get("ssdp") or []),
            "ipv6": list(h.get("ipv6") or []), "wsd": list(h.get("wsd") or []), "netbios": h.get("netbios"), "snmp": h.get("snmp"),
            "is_gateway": bool(h.get("gw")), "is_self": bool(h.get("self")), "rtt_ms": round(random.uniform(0.4, 6.0), 2)}


NEW_TODAY = ((99, 2.5 * 3600), (67, 40 * 60))     # (host number, seconds since first seen)
# The outage of the demo: the access point in the garden went down an hour ago (its controller lists it as disconnected),
# so the outage automatic has something to show; and two dependencies the user entered by hand on the component card.
OUTAGE_HOST = 4
OUTAGE_AGO_S = 3600
MANUAL_DEPS = ((12, 10, "hard", "Writes its backups there"), (15, 10, "soft", "Keeps copies of its disk images there"))


class Demo:
    absent: frozenset = frozenset()          # device ids that never answer (inventory sample: missing, gone, retired)

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.oui = registry()

    def netflow_stats(self, settings: dict) -> dict:
        """Plausible receiver figures for the settings page of the demo (no socket is opened)."""
        now = time.time()
        up = max(1, int(now - self.started)) if getattr(self, "started", None) else 3600
        flows = 1200 + up // 2
        ex = {ip: {"packets": flows // 24, "flows": flows, "last": now - 4, "version": 9}
              for ip in settings.get("netflow_exporters") or []}
        return {"listening": True, "port": settings.get("netflow_port") or 2055, "packets": flows // 24, "flows": flows,
                "booked": int(flows * 0.8), "bytes": flows * 52000, "dropped_foreign": 3, "malformed": 0, "waiting": 1,
                "refused": 0, "skipped_local": flows // 7, "skipped_self": flows // 30, "last_packet": now - 4,
                "last_flow": now - 4, "exporters": ex, "error": None}

    # ------------------------------------------------------------------ overview (start page, "Right now" rail)
    def overview_history(self, now: float, step: float, length: int) -> list:
        """A plausible day of health samples (a short wobble overnight, one brief warning) until the live loop has its own."""
        out = []
        start = now - step * (length - 1)
        for i in range(length):
            t = start + i * step
            hour = time.localtime(t).tm_hour
            level = 0
            if i in (length - 61, length - 60, length - 59):
                level = 2                                   # the camera blinked for a quarter of an hour
            elif i in (length - 140, length - 139):
                level = 1                                   # a source answered late
            online = 22 + (3 if 7 <= hour <= 23 else 0) + (i % 3 == 0)
            out.append([round(t), level, online, 27])
        return out

    def overview_changes(self, core, now: float) -> list:
        """A few recent changes of the demo network (sample names), so the rail never looks empty in the demo."""
        def dev(n):
            row = core.store.device("d-" + _hmac(n).replace(":", ""))
            return {"id": row["id"], "title": row.get("user_title") or row["title"] or row["ip"], "kind": row.get("user_kind") or row["kind"]} if row else None
        names = {row["kind"]: row["name"] for row in core.store.query("SELECT kind, name FROM integrations")}
        rows = []
        cycle = int(now // 600)
        for back, kind, n in ((95, "dev_back", 22), (460, "dev_offline", 23), (1500, "dev_back", 30), (3300, "dev_offline", 22)):
            d = dev(n)
            if d:
                rows.append({"t": now - back - (cycle % 7) * 3, "kind": kind, "data": d})
        if names.get("mqtt"):
            rows.append({"t": now - 2100, "kind": "src_ok", "data": {"id": "int:demo", "name": names["mqtt"]}})
            rows.append({"t": now - 2160, "kind": "src_fail", "data": {"id": "int:demo", "name": names["mqtt"], "key": "source.mqtt.timeout"}})
        return rows

    def networks(self) -> list:
        return [{"cidr": NET, "iface": "eth0", "self_ip": "192.168.50.11", "gateway": "192.168.50.1", "private": True,
                 "size": 256, "host_mode": True}]

    def test_details(self, kind: str) -> dict:
        return {"hub": {"version": "demo", "entities": 121, "devices": 27}, "mqtt": {"topics": len(TOPICS)},
                "snmp": {"name": "router", "descr": "Demo router", "ifaces": 4, "lldp": 1, "fdb": 0},
                "controller": {"clients": len(WIRED) - 1 + len(RADIO), "devices": 3}}.get(kind, {"queries": 9400, "blocked": 3100, "clients": 10})

    async def scan(self, plan: dict, progress, preview=None) -> dict:
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
            if phase == "ssdp" and preview is not None:       # first picture before ports and banners, like a real scan
                preview({"hosts": [dict(h, ports=[]) for h in hosts]})
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
        from . import adapter_inventory
        core.ingest_adapter(adapter_inventory.records({"demo": {"ev": controller_event(time.time())},
                                                       "demo-router": {"ev": router_topology_event(time.time())}}))
        # the demo network has been known for a few days; two devices joined today (start page: "new since yesterday")
        now = time.time()
        core.store.execute("UPDATE devices SET first_seen = first_seen - ?", (4 * 86400,))
        for n, ago in NEW_TODAY:
            core.store.execute("UPDATE devices SET first_seen = ? WHERE id = ?", (now - ago, "d-" + _hmac(n).replace(":", "")))
        core.store.set_setting("rescan_cidrs", [NET])
        self.seed_manual(core)
        seed_paths(core); seed_pairs(core)
        seed_segplan(core)
        self.seed_failure(core)

    @staticmethod
    def outage_id() -> str:
        return "d-" + _hmac(OUTAGE_HOST).replace(":", "")

    def seed_failure(self, core) -> None:
        """The outage of the demo (last seen an hour ago, the report watch saw it up before) and the manual dependencies."""
        from . import failure
        now = time.time()
        for a, b, kind, note in MANUAL_DEPS:
            ida, idb = "d-" + _hmac(a).replace(":", ""), "d-" + _hmac(b).replace(":", "")
            if core.store.device(ida) and core.store.device(idb):
                failure.set_manual(core.store, ida, idb, kind, note, now - 2 * 86400)
        ap = self.outage_id()
        if core.store.device(ap):
            core.store.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now - OUTAGE_AGO_S, ap))
            core.presence.pop(ap, None)
            watch = getattr(getattr(core, "reports", None), "watch", None)
            if watch is not None and watch.dev.get(ap) is None:
                watch.dev[ap] = "up"              # it was up before: the next round notes it offline (night report, notes)
        core.failure.invalidate()

    def seed_manual(self, core) -> None:
        for n, (parent, port) in MANUAL.items():
            child_id, parent_id = "d-" + _hmac(n).replace(":", ""), "d-" + _hmac(parent).replace(":", "")
            if core.store.device(child_id) and core.store.device(parent_id) and not core.store.one(
                    "SELECT device_id FROM phys_manual WHERE device_id=?", (child_id,)):
                core.store.execute("INSERT INTO phys_manual(device_id, parent_id, port, medium, updated) VALUES(?,?,?,?,?)",
                                   (child_id, parent_id, port, "wired", time.time() - 86400))
        core._manual_ver += 1

    def seed_integrations(self, core) -> None:
        if core.store.one("SELECT id FROM integrations LIMIT 1"):
            return
        import json as _json
        for kind, name, cfg in (("hub", "Automation hub", {"url": "http://192.168.50.14:8123", "admin_mode": True}),
                                ("mqtt", "Message broker", {"host": "192.168.50.13", "port": 1883, "tls": False}),
                                ("snmp", "Router counters", {"host": "192.168.50.1", "version": "2c"}),
                                ("controller", "Network controller", {"url": "https://192.168.50.11:8443", "username": "map-viewer",
                                                                      "site": "default"}),
                                ("nut", "Battery backup", {"host": "192.168.50.10", "port": 3493})):
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (kind, name, _json.dumps(cfg), time.time()))
        fam = _demo_family()
        if fam:                            # a device-family adapter reading the controller configuration (read only)
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (fam, "Sample controller", _json.dumps({"url": "https://192.168.50.11", "site": "default"}), time.time()))
        hv = _demo_family("guests")
        if hv:                             # a hypervisor family reading nodes, guests, firewall, storage and backups (read only)
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (hv, "Sample hypervisor", _json.dumps({"url": "https://192.168.50.15:8006"}), time.time()))
        fw = _demo_firewall()
        if fw:                             # a zone firewall family reading zones, rules, forwarding and VPN (read only)
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (fw, "Sample firewall", _json.dumps({"url": "https://192.168.50.1:4444", "username": "map-reader"}),
                                time.time()))
        gw = _demo_gateway()
        if gw:                            # a gateway firewall family with a rule API: interfaces as zones, rules, forwarding
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (gw, "Sample gateway firewall", _json.dumps({"url": "https://192.168.50.254"}), time.time()))
        rt = _demo_router()
        if rt:                            # a router family with a ubus API: firewall zones and rules, wireless networks, clients
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (rt, "Sample router", _json.dumps({"url": "https://192.168.90.1", "username": "map-reader"}),
                                time.time()))
        home = _demo_home()
        if home:                          # a home router family reading hosts and mesh topology over TR-064-style calls (read only)
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (home, "Sample home router", _json.dumps({"url": "http://192.168.178.1", "username": "lanloom"}),
                                time.time()))
        apc = _demo_apc()
        if apc:                           # an access-point controller family reading devices, ports, wireless networks and rules (read only)
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (apc, "Sample wireless controller", _json.dumps({"url": "https://192.168.50.16:8043", "site": "Default"}),
                                time.time()))
        dns = _demo_family("dns")
        if dns:                            # a DNS filter family reading totals of queries and blocked queries (read only)
            from .demo_dns import FILTER_URL
            core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                               (dns, "DNS filter", _json.dumps({"url": FILTER_URL}), time.time()))

    def report_certs(self) -> list:
        """Certificates of the demo connections (nightly report): one runs out soon."""
        return [{"id": "demo:controller", "name": "Network controller", "days": 11},
                {"id": "demo:broker", "name": "Message broker", "days": 23},
                {"id": "demo:storage", "name": "Storage", "days": 240}]

    def seed_access(self, app, now: float = None) -> None:
        """Sample users, an open invitation and a few audit entries, so the access views have something to show.
        The sample accounts cannot sign in (no usable password)."""
        if app.store.one("SELECT 1 AS x FROM users WHERE source='sample'") or app.store.one("SELECT 1 AS x FROM audit LIMIT 1"):
            return
        from . import totp as _totp
        now = time.time() if now is None else now
        day = 86400.0
        guest = app.accounts.create("guest", None, role="viewer", source="sample")
        shift = app.accounts.create("night-shift", None, role="operator", source="sample")
        app.store.execute("UPDATE users SET created=?, last_login=? WHERE id=?", (now - 20 * day, now - 2 * day, guest))
        app.store.execute("UPDATE users SET created=?, last_login=? WHERE id=?", (now - 41 * day, now - 5 * 3600, shift))
        app.vault.put("totp:%d" % shift, _totp.new_secret())
        app.factors.new_recovery(shift)
        app.invites.create("invite", "viewer", "admin", note="Hallway tablet")
        clock = app.audit.clock
        rows = [
            (41 * day, "users.joined", {"username": "night-shift", "role": "operator"}, "night-shift", "ok", {"role": "operator", "invited_by": "admin"}),
            (40.9 * day, "account.totp_on", {"username": "night-shift", "role": "operator"}, "", "ok", {}),
            (20 * day, "users.joined", {"username": "guest", "role": "viewer"}, "guest", "ok", {"role": "viewer", "invited_by": "admin"}),
            (3 * day, "auth.login_failed", None, "", "failed", {"reason": "password"}),
            (2 * day, "auth.login", {"username": "guest", "role": "viewer"}, "guest", "ok", {"method": "password", "public_computer": True}),
            (1.2 * day, "settings.change", {"username": "night-shift", "role": "operator"}, "", "denied", {"need": "admin"}),
            (5 * 3600, "auth.login", {"username": "night-shift", "role": "operator"}, "night-shift", "ok", {"method": "password+totp"}),
            (4.8 * 3600, "device.change", {"username": "night-shift", "role": "operator"}, "d-sample", "ok", {"fields": ["title"]}),
            (4.5 * 3600, "firewall.draft", {"username": "night-shift", "role": "operator"}, "", "ok", {"moves": 1}),
        ]
        try:
            for ago, action, actor, target, outcome, detail in rows:
                app.audit.clock = lambda ago=ago: now - ago
                app.audit.record(action, actor, target=target, outcome=outcome, ip="192.0.2.%d" % (20 + len(action) % 9),
                                 agent="computer", detail=detail)
        finally:
            app.audit.clock = clock
        # one API token of the operator (its value is thrown away at once; the list shows name, scope, prefix, last use)
        try:
            tok = app.tokens.create({"id": shift, "role": "operator"}, "Wall display", "read", expires_days=90)
            app.store.execute("UPDATE api_tokens SET created=?, last_used=?, last_ip=?, uses=? WHERE id=?",
                              (now - 9 * day, now - 1800, "192.0.2.41", 4120, tok["id"]))
        except Exception:
            pass

    @staticmethod
    def seed_storage(core, planned: list) -> None:
        """Counter check results (one hour this night counted twice) and a database growth history for Settings >
        Storage and the nightly report."""
        import datetime as _dt
        end = int(planned[-1] // 3600)
        results = []
        for hour in range(end - 47, end):
            wan = int((180 + 140 * math.sin(hour / 3.8)) * 1e6) + 60_000_000
            factor = 1.46 if hour == end - 4 else 0.93 + 0.05 * math.sin(hour)
            state = "double" if factor > 1.25 else "ok"
            results.append({"hour": hour, "pair": "uplink_flows", "ref": wan, "val": int(wan * factor),
                            "factor": round(factor, 3), "state": state})
            host = int(60e6 + 30e6 * math.cos(hour / 5))
            results.append({"hour": hour, "pair": "host_flows", "ref": host, "val": int(host * 0.71), "factor": 0.71, "state": "ok"})
        core.store.set_setting("counter_check", results[-48:])
        tz = core.reports.tz()
        day0 = _dt.datetime.fromtimestamp(planned[-1], tz).date()
        size = 3_100_000
        hist = []
        for k in range(14, -1, -1):
            hist.append([(day0 - _dt.timedelta(days=k)).isoformat(), size])
            size += 86_000 + (k % 3) * 9_000
        core.store.set_setting("storage_history", hist)
        core.store.set_setting("storage_state", {"t": planned[-1] - 3.2 * 3600, "dur_s": 0.41,
                                                 "removed": {"paths": 412, "scans": 3, "availability": 57, "reports": 1, "audit": 0},
                                                 "before": size + 1_400_000, "after": size, "freed": 1_400_000, "vacuum": True, "skipped": None})

    def seed_reports(self, core, now: float = None) -> None:
        """Three mornings of nightly reports from a synthetic event journal (sample names, documentation addresses)."""
        rep = core.reports
        if core.store.one("SELECT day FROM reports LIMIT 1") or not core.store.one("SELECT id FROM devices LIMIT 1"):
            return
        import datetime as _dt
        from .demo_live import live_capabilities, live_sample
        now = time.time() if now is None else now
        d0 = rep.due_day(now)
        days = [d0 - _dt.timedelta(days=2), d0 - _dt.timedelta(days=1), d0]
        planned = [rep.planned_at(d) for d in days]
        core.store.set_setting("report_epoch", planned[0] - 86400)
        dev = {h["n"]: ("d-" + _hmac(h["n"]).replace(":", ""), h) for h in HOSTS}
        ids = {row["kind"]: row["id"] for row in core.store.query("SELECT id, kind FROM integrations")}
        names = {row["kind"]: row["name"] for row in core.store.query("SELECT kind, name FROM integrations")}
        j = rep.journal

        def title(n):
            d = core.store.device(dev[n][0])
            return (d.get("user_title") or d["title"]) if d else (dev[n][1].get("title") or dev[n][1]["ip"])

        def ev(etype, at, **data):
            j.add(etype, data, at)

        def devev(kind, n, at, **extra):
            ev(kind, at, id=dev[n][0], title=title(n), kind=(core.store.device(dev[n][0]) or {}).get("kind", ""), **extra)

        h = 3600.0
        # night 1: quiet, one phone sleeps and wakes, Lanloom updated itself
        p = planned[0]
        devev("dev_offline", 22, p - 7.5 * h)
        devev("dev_back", 22, p - 0.6 * h)
        ev("app_stop", p - 4.2 * h, version="1.6.0")
        ev("app_start", p - 4.18 * h, version=_version(), prev="1.6.0", down_s=70)
        # night 2: a 14-minute internet outage and a short flap, a new device, a camera blinked, the broker dropped
        p = planned[1]
        ev("wan_down", p - 3.3 * h, name="wan")
        ev("wan_up", p - 3.3 * h + 14 * 60, name="wan")
        ev("wan_down", p - 1.1 * h, name="wan")
        ev("wan_up", p - 1.1 * h + 40, name="wan")
        ev("dev_new", p - 5.2 * h, id="d-demo-guest", title="Guest phone", ip="192.168.50.77", kind="phone", vendor="", origin="random")
        devev("dev_offline", 51, p - 5.4 * h)
        devev("dev_back", 51, p - 5.3 * h)
        for k in range(3):
            devev("dev_offline", 23, p - (8 - 2 * k) * h)
            devev("dev_back", 23, p - (7.6 - 2 * k) * h)
        if "mqtt" in ids:
            key = "int:%d" % ids["mqtt"]
            ev("src_fail", p - 3.3 * h + 60, id=key, name=names["mqtt"], key="source.mqtt.unreachable")
            ev("src_ok", p - 3.3 * h + 15 * 60, id=key, name=names["mqtt"])
        # night 3 (this morning): the hub refused the sign-in once, a camera is gone for a day, firewall rules changed
        p = planned[2]
        if "hub" in ids:
            key = "int:%d" % ids["hub"]
            ev("src_fail", p - 2.4 * h, id=key, name=names["hub"], key="source.hub.auth_failed")
            ev("src_ok", p - 2.3 * h, id=key, name=names["hub"])
        devev("dev_gone", 50, p - 1.5 * h, since=p - 25.5 * h)
        # changes written through the executor (section "Changes written"): one done, one put back on its own
        ev("fw_change", p - 9.2 * h, plan="p_demo_1", user="admin", status="done", reason="fw.res.done", n=2,
           names=["Kitchen tablet", "Office printer"], undo_of=None)
        ev("fw_change", p - 8.7 * h, plan="p_demo_2", user="admin", status="rolled_back", reason="fw.res.rolled_back_device", n=1,
           names=["Garden camera"], undo_of=None)
        devev("dev_offline", 20, p - 6 * h)
        devev("dev_back", 20, p - 1.0 * h)
        fam = _demo_family()
        sample = live_sample()
        changed = json.loads(json.dumps(sample))
        if changed.get("policies"):
            changed["policies"][-1]["action"] = "block" if changed["policies"][-1]["action"] == "allow" else "allow"
        changed["networks"].append({"id": "n_lab", "name": "Lab", "zone": changed["networks"][0]["zone"],
                                    "cidr": "192.168.77.0/24", "vlan": 77, "management": False})
        if changed.get("equipment"):
            changed["equipment"][0]["firmware"] = "4.2.2"
        hv = _demo_family("guests")
        hv_states = []
        if hv and hv in ids:                   # the hypervisor: one guest that ran yesterday is stopped this morning
            from .demo_live import hypervisor_capabilities, hypervisor_sample
            _, hv_now = hypervisor_sample(now, hv)
            hv_before = json.loads(json.dumps(hv_now))
            stopped = [g for g in hv_now.get("guests") or [] if g.get("status") == "stopped" and not g.get("template")]
            if stopped:
                next(g for g in hv_before["guests"] if g["id"] == stopped[0]["id"])["status"] = "running"
            hv_states = [hv_before, hv_before, hv_now]
        fw = _demo_firewall()
        fw_states = []
        if fw and fw in ids:                   # the firewall: a rule and a forwarding from outside arrived during the last night
            from .demo_live import firewall_capabilities, firewall_sample
            fw_before, fw_now = firewall_sample(fw), firewall_sample(fw, extra_rule=True)
            fw_states = [fw_before, fw_before, fw_now]
        gw = _demo_gateway()
        gw_states = []
        if gw and gw in ids:                 # the gateway firewall: a rule from outside arrived, a rule was switched off
            from .demo_live import gateway_capabilities, gateway_sample
            g_before, g_now = gateway_sample(gw), gateway_sample(gw, changed=True)
            gw_states = [g_before, g_before, g_now]
        rt = _demo_router()
        rt_states = []
        if rt and rt in ids:                 # the router: a rule from outside arrived, a wireless network was switched on
            from .demo_live import router_capabilities, router_sample
            r_before, r_now = router_sample(rt), router_sample(rt, changed=True)
            rt_states = [r_before, r_before, r_now]
        home = _demo_home()
        home_states = []
        if home and home in ids:             # the home router: the repeater got a new firmware, a visitor's phone joined
            from .demo_live import home_capabilities, home_sample
            h_before, h_now = home_sample(home), home_sample(home, changed=True)
            home_states = [h_before, h_before, h_now]
        apc = _demo_apc()
        apc_states = []
        if apc and apc in ids:               # the access-point controller: an access point went offline, a rule arrived
            from .demo_live import controller_capabilities, controller_sample
            w_before, w_now = controller_sample(apc), controller_sample(apc, changed=True)
            apc_states = [w_before, w_before, w_now]
        self.seed_storage(core, planned)
        dns = _demo_family("dns")
        if dns and dns in ids:                 # the DNS filter: 35 days of sums per hour and its first reading
            from . import demo_dns, dnsfilter
            demo_dns.seed_history(core, ids[dns], time.time())
            core.dns[ids[dns]] = {"state": dnsfilter.clean(demo_dns.reading(time.time(), dns)), "t": time.time()}
        prev = None
        for i, day in enumerate(days):
            if fam and fam in ids:
                core.live_cfg[ids[fam]] = {"state": sample if i < 2 else changed, "caps": live_capabilities(fam), "family": fam,
                                           "t": time.time()}
            if hv_states:
                core.live_cfg[ids[hv]] = {"state": hv_states[i], "caps": hypervisor_capabilities(hv), "family": hv, "t": time.time()}
            if fw_states:
                core.live_cfg[ids[fw]] = {"state": fw_states[i], "caps": firewall_capabilities(fw), "family": fw, "t": time.time()}
            if apc_states:
                core.live_cfg[ids[apc]] = {"state": apc_states[i], "caps": controller_capabilities(apc), "family": apc, "t": time.time()}
            if gw_states:
                core.live_cfg[ids[gw]] = {"state": gw_states[i], "caps": gateway_capabilities(gw), "family": gw, "t": time.time()}
            if home_states:
                core.live_cfg[ids[home]] = {"state": home_states[i], "caps": home_capabilities(home), "family": home, "t": time.time()}
            if rt_states:
                core.live_cfg[ids[rt]] = {"state": rt_states[i], "caps": router_capabilities(rt), "family": rt, "t": time.time()}
            since = prev["until"] if prev else planned[0] - 86400
            report = rep.assemble(since, planned[i] + 2, prev, day.isoformat(), self.report_certs())
            report["created"] = planned[i] + 2
            rep.save(report)
            if rep.plain_hook is not None:          # the AI paragraph "In plain words" (demo AI; absent without AI)
                try:
                    plain = rep.plain_hook(report)
                except Exception:
                    plain = None
                if plain:
                    plain["t"] = report["created"]
                    report["plain"] = plain
                    rep.set_plain(report["day"], plain)
            prev = report
        core.live_cfg.pop(ids.get(fam), None)
        core.live_cfg.pop(ids.get(hv), None)
        core.live_cfg.pop(ids.get(fw), None)
        core.live_cfg.pop(ids.get(apc), None)
        core.live_cfg.pop(ids.get(gw), None)
        core.live_cfg.pop(ids.get(rt), None)
        core.live_cfg.pop(ids.get(home), None)
        for day in days[:2]:
            core.store.execute("UPDATE reports SET read=? WHERE day=?", (now, day.isoformat()))
        first = rep.get(days[0].isoformat())
        for s in (first or {}).get("sections", []):
            for it in s["items"]:
                if it["point"]:
                    rep.mark_item(days[0].isoformat(), it["id"], True)

    async def run(self, core) -> None:
        """Synthetic live data: flows of this host, router counters, broker topics, hub state changes."""
        rng = self.rng
        t0 = time.time()
        hub_sent = 0
        topo_sent = 0
        live_sent = 0
        fw_history_seeded = False
        paths_sent = time.time()
        history_seeded = False
        while True:
            await asyncio.sleep(1.0)
            if not core.store.one("SELECT id FROM devices LIMIT 1"):
                continue
            now = time.time()
            if not history_seeded:
                history_seeded = True
                from . import demo_history
                demo_history.seed(core, now)              # availability and traffic history of the sample network (only when empty)
            el = now - t0
            if now - getattr(self, "_agents_t", 0) >= 10:
                from . import demo_agents
                if not getattr(self, "_agents_t", 0):
                    demo_agents.seed(core, now)           # sample host agents and a container source (E4)
                    from . import demo_hosthealth
                    demo_hosthealth.seed(core, now)       # seven days of host samples with an early warning (W1)
                    from . import demo_controlroom
                    demo_controlroom.seed(core, now)      # internet history, UPS night, backup chains (S9-S12)
                self._agents_t = now
                demo_agents.tick(core, now, el, rng)
                from . import demo_controlroom
                demo_controlroom.tick(core, now, el)
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
            if now - paths_sent >= 60:
                paths_sent = now
                seed_paths(core, now); seed_pairs(core, now)              # a demo set up through the wizard gets its history on the first minute
                seed_segplan(core)
                book_paths_minute(core, now, rng)
            fam, hv, fw = _demo_family(), _demo_family("guests"), _demo_firewall()
            if now - live_sent >= 60:
                live_sent = now
                from .demo_live import (firewall_capabilities, firewall_sample, hypervisor_capabilities, hypervisor_sample,
                                        live_capabilities, live_sample)
                if fw and fw in ids:
                    core.on_event("int:%d" % ids[fw], ids[fw], {"type": "config", "source": "family", "family": fw, "t": now,
                                                               "state": firewall_sample(fw, extra_rule=True),
                                                               "capabilities": firewall_capabilities(fw)})
                gw = _demo_gateway()
                if gw and gw in ids:
                    from .demo_live import gateway_capabilities, gateway_sample
                    core.on_event("int:%d" % ids[gw], ids[gw], {"type": "config", "source": "family", "family": gw, "t": now,
                                                               "state": gateway_sample(gw, changed=True),
                                                               "capabilities": gateway_capabilities(gw)})
                rt = _demo_router()
                if rt and rt in ids:
                    from .demo_live import router_capabilities, router_sample
                    core.on_event("int:%d" % ids[rt], ids[rt], {"type": "config", "source": "family", "family": rt, "t": now,
                                                               "state": router_sample(rt, changed=True),
                                                               "capabilities": router_capabilities(rt)})
                home = _demo_home()
                if home and home in ids:
                    from .demo_live import home_capabilities, home_sample
                    core.on_event("int:%d" % ids[home], ids[home], {"type": "config", "source": "family", "family": home, "t": now,
                                                                   "state": home_sample(home, changed=True),
                                                                   "capabilities": home_capabilities(home)})
                apc = _demo_apc()
                if apc and apc in ids:
                    from .demo_live import controller_capabilities, controller_sample
                    core.on_event("int:%d" % ids[apc], ids[apc], {"type": "config", "source": "family", "family": apc, "t": now,
                                                                   "state": controller_sample(apc, changed=True),
                                                                   "capabilities": controller_capabilities(apc)})
                dns = _demo_family("dns")
                if dns and dns in ids:
                    from .demo_dns import reading as dns_reading
                    core.on_event("int:%d" % ids[dns], ids[dns], {"type": "dns", "source": "family", "family": dns, "t": now,
                                                                 "state": dns_reading(now, dns)})
                if fam and fam in ids:
                    if not fw_history_seeded:
                        fw_history_seeded = True
                        from .demo_live import seed_history
                        seed_history(core, "int:%d" % ids[fam], now)
                        core.roamwatch.moves = [[now - 600 * k - 400, _mac(AP_1 if k % 2 else AP_2), _mac(AP_2 if k % 2 else AP_1)] for k in range(1, 8)]
                    core.on_event("int:%d" % ids[fam], ids[fam], {"type": "config", "source": "family", "family": fam, "t": now,
                                                                 "state": live_sample(now), "capabilities": live_capabilities(fam)})
                if hv and hv in ids:
                    topo, st = hypervisor_sample(now, hv)
                    core.on_event("int:%d" % ids[hv], ids[hv], topo)
                    core.on_event("int:%d" % ids[hv], ids[hv], {"type": "config", "source": "family", "family": hv, "t": now,
                                                               "state": st, "capabilities": hypervisor_capabilities(hv)})
            if now - topo_sent >= 30:
                topo_sent = now
                outage = self.outage_id()
                for row in core.store.query("SELECT id FROM devices"):     # simulated presence check: every demo device answers
                    if row["id"] not in self.absent and row["id"] != outage:   # ... except the ones kept away on purpose
                        core.presence[row["id"]] = now
                if "controller" in ids:
                    core.on_event("int:%d" % ids["controller"], ids["controller"], controller_event(now))
                if "snmp" in ids:
                    core.on_event("int:%d" % ids["snmp"], ids["snmp"], router_topology_event(now))
            if "mqtt" in ids:
                topics = []
                for root, rate, size in TOPICS:
                    n = sum(1 for _ in range(int(rate * 2)) if rng.random() < 0.5) if rate >= 1 else (1 if rng.random() < rate else 0)
                    topics.append([root, n, n * size])
                core.on_event("int:%d" % ids["mqtt"], ids["mqtt"], {"type": "mqtt", "t": now, "dt": 1.0, "topics": topics,
                                                                   "broker": {"clients": 14}})
            if "hub" in ids:
                from . import demo_hub
                hid = ids["hub"]
                key = "int:%d" % hid
                if hub_sent == 0 or now - hub_sent > 600:
                    core.on_event(key, hid, self._snapshot(now))
                    if hub_sent == 0:
                        for frame in demo_hub.history_frames(now, rng) + demo_hub.change_frames(now):
                            core.on_event(key, hid, frame)
                    core.on_event(key, hid, demo_hub.deps_frame(now))
                    hub_sent = now
                if now - getattr(self, "_hub_bridge_t", 0) >= 60:
                    self._hub_bridge_t = now
                    core.on_event(key, hid, demo_hub.bridge_frame(now))
                if now - getattr(self, "_hub_trig_t", 0) >= 10:
                    self._hub_trig_t = now
                    frame = demo_hub.triggers_frame(now, rng)
                    if frame:
                        core.on_event(key, hid, frame)
                for _ in range(rng.randint(1, 4)):
                    area = rng.choice(AREAS)
                    core.on_event(key, hid, {"type": "hub_state", "entity_id": "sensor.%s_%d" % (area[0], rng.randint(1, area[3])),
                                             "state": "%.1f" % rng.uniform(18, 24), "t": now})
                for frame in demo_hub.radio_tick(now, rng):
                    core.on_event(key, hid, frame)
                if rng.random() < 0.3:
                    eid = rng.choice(["light.living_room_2", "light.hallway_2", "switch.kitchen_3", "switch.office_3"])
                    ev = {"type": "hub_state", "entity_id": eid, "state": rng.choice(["on", "off"]), "t": now}
                    by = demo_hub.by_for(eid, rng, ev["state"])
                    if by:
                        ev["by"] = by
                    core.on_event(key, hid, ev)

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
        from . import demo_hub
        return demo_hub.extend_snapshot({"type": "hub_snapshot", "t": now, "devices": devices, "entities": entities,
                                         "areas": [{"id": a[0], "name": a[1]} for a in AREAS]}, now)

    def hub_traces(self, entity_id: str):
        from . import demo_hub
        return demo_hub.traces(entity_id, time.time())

    def hub_history(self, entity: dict, rng: str, numeric: bool, now: float) -> dict:
        from . import demo_hub
        return demo_hub.history(entity, rng, numeric, now)


def _demo_family(area: str = "ssids", without: str = None):
    """The first device-family adapter offered in the integration dialog that reads ``area`` (and not ``without``):
    ``ssids`` for the controller (demo shows it as "Sample controller"), ``guests`` for the hypervisor ("Sample
    hypervisor"), ``port_forwards`` without ``ssids`` for the zone firewall ("Sample firewall"), which must also
    speak the query reply format the sample firewall is written in."""
    from . import integrations
    fams = [m.family for m in integrations.BUILTIN if m.selectable and m.read and area in (m.capabilities.get("read") or [])
            and not (without and without in (m.capabilities.get("read") or []))
            # the sample controller has a zone firewall and manages switch ports (a router family reads zones and wireless too)
            and (area != "ssids" or {"zones", "port_profiles"} <= set(m.capabilities.get("read") or []))]
    if area == "port_forwards":
        fams = [f for f in fams if hasattr(integrations.load_model(f), "parse_reply")]
    return fams[0] if fams else None


def _demo_firewall():
    return _demo_family("port_forwards", without="ssids")


def _demo_gateway():
    """The gateway firewall family with a rule API (reads rules and forwarding, no wireless networks, and answers in
    JSON rather than a query reply): "Sample gateway firewall"."""
    from . import integrations
    fams = [m.family for m in integrations.BUILTIN if m.selectable and m.read
            and {"policies", "port_forwards"} <= set(m.capabilities.get("read") or [])
            and "ssids" not in (m.capabilities.get("read") or [])
            and not hasattr(integrations.load_model(m.family), "parse_reply")]
    return fams[0] if fams else None


def _demo_router():
    """The router family with a ubus API (reads firewall rules and wireless networks but manages no switch ports):
    "Sample router"."""
    from . import integrations
    fams = [m.family for m in integrations.BUILTIN if m.selectable and m.read
            and {"policies", "ssids", "zones"} <= set(m.capabilities.get("read") or [])
            and "port_profiles" not in (m.capabilities.get("read") or [])]
    return fams[0] if fams else None


def _demo_home():
    """The home router family (reads hosts, mesh topology and the internet connection, but no rules): "Sample home router"."""
    from . import integrations
    fams = [m.family for m in integrations.BUILTIN if m.selectable and m.read
            and {"clients", "topology", "networks", "wans"} <= set(m.capabilities.get("read") or [])
            and not {"acl", "policies", "zones", "ssids", "guests", "dns"} & set(m.capabilities.get("read") or [])]
    return fams[0] if fams else None


def _demo_apc():
    """The access-point controller family (reads rules and wireless networks but has no zone firewall): "Sample wireless controller"."""
    return _demo_family("acl", without="zones")



def _version() -> str:
    from . import __version__
    return __version__
