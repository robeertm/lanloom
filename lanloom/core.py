"""Application core: inventory, scans, presence checks, live sources and the 1-second live loop."""
import asyncio
import ipaddress
import json
import logging
import math
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import adapter_inventory, availability as availability_mod, services as services_mod, portfront, portnotify, classify, counters as counters_mod, datapaths, dnsfilter, geo as geo_mod, hub_privacy, hubauto, hubbridge, hubmerge, hubsensors, identity, live, netflow, oui as oui_mod, textfmt, topology, trafficlog as trafficlog_mod, zones as zones_mod
from .agents import AgentHub
from .crypto import Vault
from .firewall import insight as fw_insight
from .scan import protect as protect_mod
from .store import Store

log = logging.getLogger("lanloom")

PRESENCE_INTERVAL = 120.0
FLOW_KEEP = 3600.0
PEER_TAU = 60.0          # weight of an outside peer / a seen port decays with this time constant (s) …
PEER_KEEP = 600.0        # … and entries older than this are dropped
PEER_MAX = 14            # outside peers per frame (the end-mark list shows 14 rows)
DEFAULT_SETTINGS = {
    "rescan_hours": 6,          # 0 = never; automatic rescans are always gentle
    "rescan_cidrs": [],
    "update_check": True,
    "language": None,
    "setup_done": False,
    "uplink_iface": "",
    "rate_unit": "bytes",       # traffic rates as bytes per second (MB/s) or bits per second (Mbit/s)
    "home_country": "",         # data paths: ISO code of the home country ("" = the browser's region)
    "paths_own_networks": [],   # data paths: own networks and tunnels beyond the LAN (never "unassigned")
    "paths_no_internet": [],    # data paths: local networks that should not reach the internet
    "scan_snmp_public": False,  # scans may ask devices with the default read community (user choice, off by default)
    "port_noise": False,        # exact port counters: take the background noise every device of a network sees out of each one
    "service_probe": True,      # look at the known services and web interfaces every 3 minutes (never anything else, never a login)
    "adapter_inventory": True,  # devices only an adapter knows (other VLANs, switch address tables) join the inventory
    "paths_detail": False,      # data paths: cities and network operators (fetches two files from their source; off by default)
    "netflow_enabled": False,   # data paths: NetFlow/IPFIX receiver (off by default; needs at least one allowed exporter)
    "netflow_port": netflow.DEFAULT_PORT,
    "netflow_exporters": [],    # addresses allowed to send flows; packets from anyone else are dropped
    "netflow_lan_networks": [], # extra own LAN ranges for NetFlow (the detected local networks always count)
    "paths_routes": False,      # data paths: network routes (sends a few probe packets towards the top destinations; off by default)
    "paths_routes_targets": 5,  # how many destination groups are measured (1..20)
    "paths_routes_interval_h": 6,   # each at most every N hours (1..168)
    "paths_routes_peeringdb": False,  # exchange points from PeeringDB (fetches three public lists; off by default)
}
# settings of the instance, not of one site (a further site reads and writes them in the home store)
INSTANCE_SETTINGS = frozenset({"update_check", "language", "setup_done", "rate_unit", "paths_detail", "paths_routes",
                               "paths_routes_targets", "paths_routes_interval_h", "paths_routes_peeringdb"})
NETFLOW_LAN_REFRESH = 300.0

INTEGRATION_FIELDS = {
    # kind -> (plain config fields, secret fields)
    "hub": (("url", "allow_self_signed", "admin_mode") + tuple(hub_privacy.GROUP_FIELDS), ("token",)),
    "mqtt": (("host", "port", "username", "tls", "allow_self_signed"), ("password",)),
    "snmp": (("host", "version", "user", "auth_proto", "priv_proto", "uplink"), ("community", "auth_key", "priv_key")),
    "controller": (("url", "username", "site", "style", "allow_self_signed"), ("password",)),
    "nut": (("host", "port", "ups"), ()),
}


def integration_fields(kind: str):
    """(plain config fields, secret fields) of a generic source kind or of a built-in family with a read half."""
    if kind in INTEGRATION_FIELDS:
        return INTEGRATION_FIELDS[kind]
    from . import integrations
    try:
        m = integrations.get(kind)
    except KeyError:
        return None
    if not m.read or not m.selectable:
        return None
    return tuple(m.config_fields), tuple(m.secret_fields)


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback and not addr.is_link_local


_LAN_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]


def _is_outside(ip: str) -> bool:
    """A peer beyond the local network (no tile on the map): not private LAN space, loopback, link-local or multicast."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return False
    return not any(addr.version == n.version and addr in n for n in _LAN_NETS)


class Core:
    def __init__(self, cfg, store: Store, vault: Optional[Vault], site: str = "home", remote: bool = False, shared=None):
        """``site``: the site this core belongs to (lanloom/sites.py). ``remote``: a site away from this host - it
        never uses this host's interfaces, presence check or NetFlow receiver, and scans only ranges the user agreed to.
        ``shared``: the home core, whose read-only lookups (vendor list, geo table) a further site uses as well."""
        self.cfg = cfg
        self.store = store
        self.vault = vault
        self.site_id = site
        self.remote = bool(remote)
        self.site_hub = None               # the site list of the instance (set by the app)
        self.people_store = None           # further sites: accounts, personal rules and push devices live in the home store
        self.people_vault = None
        self.instance_store = None         # further sites: instance-wide settings (language, updates ...) live there too
        self.channel_reports = None        # further sites: deliveries use the channels set up at home
        self.demo = None
        self.scan_state = {"state": "idle"}
        self._scan_task: Optional[asyncio.Task] = None
        self._tasks: list = []
        self._source_tasks: dict = {}      # integration id or "host" -> task
        self._hub_sources: dict = {}       # integration id -> running hub source (traces on request)
        self.source_status: dict = {}      # key -> {"ok","key","t"}
        self.cadence: dict = {}            # source key -> live.Cadence
        self.quant = live.Quantizer()
        self.ring = live.Ring(600)
        self.clients: set = set()
        self.ip_to_device: dict = {}
        self.self_ip: Optional[str] = None
        self.self_device: Optional[str] = None      # id of this host's own entry (flows of the host are booked on it)
        self.gateway_ip: Optional[str] = None
        self.presence: dict = {}           # device id -> last confirmed alive (ts)
        self.flow_seen: dict = {}          # device id -> last ts with host flow bytes
        self.outside_peers: dict = {}      # peer ip -> {"out", "in", "w", "t"} decaying bytes of this host with peers beyond the LAN
        self.flow_ports: dict = {}         # device id -> {port: [decaying count, last ts]} target ports seen in host flows
        self.pending_bytes: dict = {}      # trace id -> [b0, b1]
        self.spread: dict = {}             # trace id -> [bytes0, bytes1, seconds left] (counters polled every N s)
        self.self_iface: dict = {}         # iface -> [tx/s, rx/s]
        self.hub: dict = {}                # integration id -> latest snapshot (+ state cadence)
        self.mqtt: dict = {}               # integration id -> {"topics": {root: [msgs/s, bytes/s, last]}, "t"}
        self.snmp: dict = {}               # integration id -> latest snmp frame
        self.topic_order: dict = {}        # integration id -> stable list of shown topic roots
        self.topo_obs: dict = {}           # source key -> {"integ": id, "ev": latest topology event}
        self._adapter_t = 0.0              # last time the adapters' devices were booked into the inventory
        self.live_cfg: dict = {}           # integration id -> latest neutral configuration of a family read half
        self.portwatch = portfront.PortWatch(store)      # link drops, error episodes and rates per switch port (S4)
        self.roamwatch = portfront.RoamWatch()           # wireless clients per access point and the moves between them
        self.hitlog = fw_insight.HitLog(store)           # firewall hit counters over time: blocked today, hits of the last hour (S6)
        self._port_open: set = set()       # ports announced as unstable and not yet calm again
        self.port_exact: dict = {}         # physical trace id -> last time its bytes came from the port counters of a switch
        self._ports_light: dict = {}       # integration id -> last time the light poll carried ports
        self._topo_cache = None            # (key, resolved topology)
        self._topo_sig = ""
        self._manual_ver = 0
        self.structure_version = 0
        self.last_frame: dict = {}
        data_dir = getattr(cfg, "data_dir", None)
        if shared is not None:
            self.geo, self.oui = shared.geo, shared.oui      # one copy of the read-only tables for all sites
        else:
            live_data = data_dir is not None and not getattr(cfg, "demo", False) and not getattr(cfg, "probe", False)
            self.geo = geo_mod.Geo(detail_dir=(data_dir / "geo") if live_data else None)
            self.oui = oui_mod.Oui(extra=(data_dir / "oui" / "oui.bin.gz") if data_dir else None)
        self._ident_cache = None           # (key, identity.groups result)
        self._merge_cache = None           # (key, hubmerge.match result)
        self._radio_cache: dict = {}       # integration id -> (key, monotonic time, radio view)
        self.paths = datapaths.DataPaths(store, self.geo, self.settings)
        from .routewatch import RouteWatch
        live_routes = data_dir is not None and not getattr(cfg, "demo", False) and not getattr(cfg, "probe", False)
        self.routes = RouteWatch(self, (data_dir if live_routes else None))     # network routes (DW2), off by default
        self.paths.route_sink = self.routes
        from .cables import CableData                   # real cables (DW3): bundled cable file (ODbL), an admin import
        if shared is not None and getattr(shared, "cables", None) is not None:
            self.cables = shared.cables
        else:
            self.cables = CableData((data_dir / "cables") if live_routes else None)
        self._cable_cache: dict = {}       # (route key, measured at, data version) -> estimate, crossings and path (memory only)
        self._infra = None                 # infraroute.InfraRoutes (view "Real cables": every data path over infrastructure), built on first use
        self.availability = availability_mod.Availability(store)     # 5-minute slots per device/connection (S2)
        self.services = services_mod.Services(self)                  # services, web interfaces, link probe, certificates (E5)
        self.traffic_log = trafficlog_mod.TrafficLog(store)           # bytes per minute per trace (plot ranges up to 7 days)
        self.dns: dict = {}                                           # DNS filters: integration id -> {"state", "t"} (last reading)
        self.dns_log = dnsfilter.DnsLog(store)                        # their sums per hour and day (never single queries)
        self.ip6_to_device: dict = {}
        self._nf_lan = (0.0, [])           # (ts, detected local networks) for the NetFlow direction check
        self.netflow = netflow.Collector(self.paths, self._netflow_device, self._netflow_lan, self._netflow_skip)
        from .nightly import Reports
        self.reports = Reports(self, getattr(cfg, "data_dir", None))
        from .notify import Notifier
        self.notify = Notifier(self)
        self.agents = AgentHub(self)                    # host agents and container sources (E4)
        from .hosthealth import HostHealth
        self.hosthealth = HostHealth(self)              # host gauges, samples and early warnings (W1)
        from .controlroom.room import ControlRoom
        self.controlroom = ControlRoom(self)            # control room: internet watch, UPS, storage box, backup chains (S9-S12)
        from .energy import EnergyBook
        self.energy = EnergyBook(self)                  # energy flow from the hub's power sensors, price, CO2, day totals (S17)
        self.reports.journal.listeners.append(self.notify.on_journal)
        from .firewall.drift import Drift
        self.fwdrift = Drift(store, lambda: self.reports.journal)    # rule changes made outside Lanloom -> event journal (W3)
        self.reports.on_certs = self.notify.on_certs
        from .failure import Failure
        self.failure = Failure(self)                    # failure chain, manual dependencies, outage automatic (S5)
        from .resilience import Resilience
        self.resilience = Resilience(self)              # risk lens, weak spots, emergency scenarios, layers (S13)
        from .incidents import Incidents
        self.incidents = Incidents(self)                # incident files: episodes bundled over the failure graph (S8)
        from .chronicle import Chronicle
        self.chronicle = Chronicle(self)                # timeline & data state, collected once a day (S8)
        self.reports.report_listeners.append(self._after_report_chronicle)
        from .storage import Storage
        self.storage = Storage(self)                    # retention, nightly clean-up, sizes per area
        self.counters = counters_mod.CounterCheck(self)  # hourly reconciliation of meters that see the same bytes
        self.map_version = 0                            # change channel: rises when what the map shows changed
        self._map_sig = None
        self._map_sig_t = 0.0
        self._view_cache: dict = {}                     # device id -> (row signature, view without status)
        from .overview import Overview
        self.overview = Overview(self)
        from .inventory import Inventory
        self.inventory = Inventory(self)                # own fields, life cycle states, end-of-updates hints
        from .vulns import Vulns
        self.vulns = Vulns(self)                        # vulnerability hints from the offline feed
        self._refresh_index()
        try:
            self.rekey_stale_paths()
        except Exception:
            log.exception("data paths of former device ids could not be moved (kept as they are)")

    REKEY_BACKUP = "paths_rekey_backup"
    REKEY_KEEP = 5                  # moves whose copy is kept
    REKEY_ROWS = 2000               # rows per table and move in that copy

    def rekey_stale_paths(self) -> int:
        """Data paths stored under an id no device has any more move to the device that owns that id now (lanloom#13):
        an address id ("d-ip-<address>") to the entry with that address - the device got its hardware-address id before
        the move was done on the spot - and the "self" placeholder to this host's own entry. Ids that match no device stay
        as they are. Before anything moves, a copy of the rows is kept in the settings (the last few moves). Idempotent:
        a second run finds nothing to do. Returns the rows moved."""
        known = {d["id"] for d in self.store.devices()}
        plan = []
        for old in self.paths.stale_devices(known):
            if old == "self":
                target = self.self_device
            else:
                ip = datapaths.ip_of_id(old)
                target = (self.ip_to_device.get(ip) or self.ip6_to_device.get(ip)) if ip else None
            if target and target != old and target in known:
                plan.append((old, target))
        if not plan:
            return 0
        copy = self.store.get_setting(self.REKEY_BACKUP, [])
        copy = copy if isinstance(copy, list) else []
        for old, new in plan:
            copy.append({"t": round(time.time(), 1), "old": old, "new": new,
                         "rows": self.paths.snapshot_device(old, self.REKEY_ROWS)})
        self.store.set_setting(self.REKEY_BACKUP, copy[-self.REKEY_KEEP:])
        moved = 0
        for old, new in plan:
            moved += self.paths.rename_device(old, new)
        log.info("data paths: moved %d rows of %d former device ids onto their current entries", moved, len(plan))
        return moved

    def _after_report_chronicle(self, report) -> None:
        """After every nightly report: incidents and the chronicle are collected in the background (never in the loop)."""
        def run():
            try:
                self.chronicle.collect()
            except Exception:
                log.exception("chronicle collection failed")
        threading.Thread(target=run, name="chronicle", daemon=True).start()

    # ------------------------------------------------------------------ settings
    def _setting_store(self, key: str) -> Store:
        """Instance-wide settings (language, update check ...) live in the home store; the rest belongs to the site."""
        if key in INSTANCE_SETTINGS and self.instance_store is not None:
            return self.instance_store
        return self.store

    def settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        for key in DEFAULT_SETTINGS:
            out[key] = self._setting_store(key).get_setting(key, DEFAULT_SETTINGS[key])
        if self.remote:
            out["netflow_enabled"] = False                  # the receiver belongs to the home site
        return out

    def rate_unit(self) -> str:
        unit = self._setting_store("rate_unit").get_setting("rate_unit", "bytes")
        return unit if unit in live.RATE_UNITS else "bytes"

    def update_settings(self, patch: dict) -> dict:
        allowed = {"rescan_hours": int, "update_check": bool, "language": (str, type(None)), "setup_done": bool,
                   "uplink_iface": str, "rate_unit": str, "home_country": str, "paths_own_networks": list,
                   "paths_no_internet": list, "netflow_enabled": bool, "netflow_port": int, "netflow_exporters": list,
                   "netflow_lan_networks": list, "adapter_inventory": bool, "port_noise": bool, "service_probe": bool,
                   "paths_detail": bool, "paths_routes": bool, "paths_routes_targets": int, "paths_routes_interval_h": int,
                   "paths_routes_peeringdb": bool}
        nets = {}
        for key in ("paths_own_networks", "paths_no_internet", "netflow_lan_networks"):   # all or nothing: check first
            if key in patch:
                nets[key] = datapaths.parse_networks(patch[key])
                if nets[key] is None:
                    raise ValueError("settings.paths.invalid_network")
        if "netflow_exporters" in patch:
            nets["netflow_exporters"] = [] if patch["netflow_exporters"] == [] else netflow.parse_exporters(patch["netflow_exporters"])
            if nets["netflow_exporters"] is None:
                raise ValueError("settings.netflow.invalid_exporter")
        if "netflow_port" in patch:
            nets["netflow_port"] = netflow.parse_port(patch["netflow_port"])
            if nets["netflow_port"] is None:
                raise ValueError("settings.netflow.invalid_port")
        nf_keys = ("netflow_enabled", "netflow_port", "netflow_exporters", "netflow_lan_networks")
        if self.remote and patch.get("netflow_enabled") is True:
            raise ValueError("site.err.netflow_home")       # one receiver per instance, at the home site
        if any(k in patch for k in nf_keys):
            enabled = patch.get("netflow_enabled", self.store.get_setting("netflow_enabled", False)) is True
            exporters = nets.get("netflow_exporters", self.store.get_setting("netflow_exporters", []) or [])
            if enabled and not exporters:
                raise ValueError("settings.netflow.exporter_required")   # never a receiver that accepts anyone
        for key, value in patch.items():
            if key in allowed and isinstance(value, allowed[key]):
                if key in nets:
                    value = nets[key]
                if key == "home_country":
                    value = value.strip().upper()
                    if value and not re.fullmatch(r"[A-Z]{2}", value):
                        continue
                if key == "rescan_hours" and value not in (0, 1, 3, 6, 12, 24):
                    continue
                if key == "language" and value not in (None, *textfmt.languages()):
                    continue
                if key == "rate_unit" and value not in live.RATE_UNITS:
                    continue
                if key == "paths_routes_targets" and not 1 <= value <= 20:
                    raise ValueError("settings.routes.invalid_targets")
                if key == "paths_routes_interval_h" and not 1 <= value <= 168:
                    raise ValueError("settings.routes.invalid_interval")
                if key in ("paths_routes", "paths_routes_peeringdb") and value and self.remote:
                    raise ValueError("site.err.routes_home")      # measured from the host Lanloom runs on, at the home site
                self._setting_store(key).set_setting(key, value)
        if any(k in patch for k in nf_keys):
            self._netflow_schedule()
        if "paths_detail" in patch and isinstance(patch["paths_detail"], bool):
            self.geo.city.configure(patch["paths_detail"])
        if any(k.startswith("paths_routes") for k in patch):
            self.routes.apply_settings()
        return self.settings()

    # ------------------------------------------------------------------ NetFlow / IPFIX receiver (data paths)
    def _netflow_device(self, ip: str) -> Optional[str]:
        return self.ip_to_device.get(ip) or self.ip6_to_device.get(ip)

    def _netflow_lan(self) -> list:
        ts, detected = self._nf_lan
        now = time.time()
        if now - ts > NETFLOW_LAN_REFRESH:
            detected = []
            for n in self.networks():
                try:
                    detected.append(ipaddress.ip_network(n.get("cidr") or "", strict=False))
                except (ValueError, TypeError):
                    pass
            self._nf_lan = (now, detected)
        extra = []
        for v in self.store.get_setting("netflow_lan_networks", []) or []:
            try:
                extra.append(ipaddress.ip_network(v, strict=False))
            except (ValueError, TypeError):
                pass
        return detected + extra

    def _netflow_skip(self) -> set:
        """The host source books this host's own flows exactly; NetFlow must not count them a second time."""
        return {self.self_ip} if self.self_ip and not self.demo else set()

    def _netflow_schedule(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._tasks.append(loop.create_task(self.netflow_apply()))

    async def netflow_apply(self) -> None:
        """Open, move or close the UDP receiver to match the settings (never in a probe start or the demo)."""
        s = self.settings()
        exporters = list(s.get("netflow_exporters") or [])
        port = s.get("netflow_port") or netflow.DEFAULT_PORT
        on = bool(s.get("netflow_enabled")) and bool(exporters) and not self.cfg.probe and not self.demo and not self.remote
        self.netflow.configure(exporters if on else [], port)
        if not on:
            self.netflow.stop()
            return
        st = self.netflow.stats()
        if not st["listening"] or st["port"] != port:
            await self.netflow.start(port=port)

    def netflow_status(self) -> dict:
        s = self.settings()
        out = self.netflow.stats()
        out["enabled"] = bool(s.get("netflow_enabled"))
        if self.demo and out["enabled"]:
            out = dict(out, **self.demo.netflow_stats(s)) if hasattr(self.demo, "netflow_stats") else out
        return out

    def paths_source(self) -> dict:
        """Where the data paths come from: this host's connections, NetFlow from the allowed exporters, or both."""
        nf = self.netflow_status()
        out = {"kind": "host", "unbooked": self.paths.unbooked}
        if nf["enabled"]:
            out["kind"] = "both"
            out["netflow"] = {"exporters": sorted(nf.get("exporters") or {}), "listening": nf["listening"],
                              "last_flow": nf.get("last_flow"), "error": nf.get("error")}
        return out

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.cfg.probe:
            return            # probe start of an update candidate: serve the UI only, no discovery, no sources
        self.geo.start()
        if self.settings().get("paths_detail") and not self.remote:
            self.geo.city.configure(True)         # was switched on before: map the file, check once a day
        if not self.demo and not self.remote:
            self.routes.start()                   # network routes: the loop does nothing while the option is off
        self.oui.start()
        self.reports.on_start()
        if self.demo:
            self._tasks.append(asyncio.create_task(self.demo.run(self)))
            self._tasks.append(asyncio.create_task(self.services.loop()))
        else:
            if not self.remote:
                # this host's own interfaces and the presence check belong to the site Lanloom runs in; a remote site
                # is seen only through its controller or a host agent there (never by probing across a router)
                self._start_source("host", "host", {})
            for integ in self.integrations():
                if integ["enabled"]:
                    self._start_integration(integ)
            if not self.remote:
                self._tasks.append(asyncio.create_task(self._presence_loop()))
            self._tasks.append(asyncio.create_task(self._rescan_loop()))
            self._tasks.append(asyncio.create_task(self.services.loop()))
            await self.netflow_apply()
            self._tasks.append(asyncio.create_task(self.storage.loop()))
            self._tasks.append(asyncio.create_task(self.inventory.loop()))
            self._tasks.append(asyncio.create_task(self.vulns.loop()))
        self._tasks.append(asyncio.create_task(self._tick_loop()))
        self._tasks.append(asyncio.create_task(self.agents.loop()))
        self._tasks.append(asyncio.create_task(self.hosthealth.loop()))
        self._tasks.append(asyncio.create_task(self.controlroom.loop()))
        self._tasks.append(asyncio.create_task(self.reports.loop()))
        self._tasks.append(asyncio.create_task(self.notify.loop()))

    async def stop(self) -> None:
        self.netflow.stop()
        self.routes.stop()
        for task in list(self._tasks) + list(self._source_tasks.values()) + ([self._scan_task] if self._scan_task else []):
            task.cancel()
        for task in list(self._tasks) + list(self._source_tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._source_tasks.clear()
        for part in (self.availability, self.traffic_log, self.paths, self.energy):
            try:
                part.flush()
            except Exception:
                log.exception("history flush at stop failed")
        try:
            self.agents.save(force=True)          # pending host states and "last seen" times (B9)
        except Exception:
            log.exception("agent save at stop failed")

    # ------------------------------------------------------------------ inventory
    def _refresh_index(self) -> None:
        self.ip_to_device = {}
        ip6 = {}
        self_dev = None
        for dev in self.store.devices():
            if dev["ip"]:                  # a device only an adapter knows may have no address yet
                self.ip_to_device[dev["ip"]] = dev["id"]
            if dev["data"].get("is_self") and (self_dev is None or self_dev.startswith("d-ip-")):
                self_dev = dev["id"]       # the entry with the hardware address wins over an address-only leftover
            data = dev["data"]
            for a in (data.get("ipv6") or [])[:16]:
                try:
                    ip6.setdefault(str(ipaddress.ip_address(str(a).split("%", 1)[0])), dev["id"])
                except ValueError:
                    pass
            if data.get("is_self"):
                self.self_ip = dev["ip"]
            if data.get("is_gateway"):
                self.gateway_ip = dev["ip"]
        self.ip6_to_device = ip6
        self.self_device = self_dev
        self.structure_version += 1

    def devices(self) -> list:
        """Views of all devices. The part of a view that only depends on the stored row (identity, services, ...) is
        kept per device and rebuilt only when the row or the device structure changed; status and AI hints are always
        fresh. Callers get their own dicts."""
        last = self._last_scan_id()
        groups_key = (self.structure_version, id(self.registry()), id(getattr(self.registry(), "db", None)))
        cache, out = self._view_cache, []
        for r in self.store.query("SELECT * FROM devices ORDER BY ip"):
            sig = (r["last_seen"], r["last_scan"], r["user_title"], r["user_kind"], r["hidden"], r["kind"], r["title"],
                   r["confidence"], r["mac"], r["ip"], r["data"], groups_key)
            hit = cache.get(r["id"])
            if hit and hit[0] == sig:
                view = dict(hit[1])
            else:
                r["data"] = json.loads(r["data"])
                static = self._static_view(r)
                cache[r["id"]] = (sig, static)
                view = dict(static)
            view["status"] = self.device_status(r, last)
            out.append(view)
        self._apply_hub_kinds(out)
        out = [self._with_hint(v) for v in out]
        if len(cache) > len(out) + 64:
            keep = {v["id"] for v in out}
            self._view_cache = {k: v for k, v in cache.items() if k in keep}
        return out

    # ------------------------------------------------------------------ identity (manufacturer, interfaces)
    def registry(self) -> oui_mod.Oui:
        """The manufacturer table: the bundled one, or the demo's fictional one."""
        reg = getattr(self.demo, "oui", None)
        return reg if isinstance(reg, oui_mod.Oui) else self.oui

    def identity_rules(self) -> dict:
        rules = self.store.get_setting("identity", {}) or {}
        return {"split": [list(p) for p in rules.get("split") or [] if isinstance(p, list) and len(p) == 2],
                "join": [list(p) for p in rules.get("join") or [] if isinstance(p, list) and len(p) == 2]}

    def identity_groups(self) -> dict:
        """One device, several interfaces (see identity.groups); cached until devices or rules change."""
        rules = self.identity_rules()
        topo = self._topo_cache[1] if self._topo_cache else None
        key = (self.structure_version, json.dumps(rules, sort_keys=True), id(topo))
        if self._ident_cache and self._ident_cache[0] == key:
            return self._ident_cache[1]
        links = (topo or {}).get("links") or {}
        rows = []
        for d in self.store.devices():
            link = links.get(d["id"]) or {}
            adp = d["data"].get("adapter") or {}
            if adp.get("parent_mac") and adp.get("port"):      # where the adapter saw the address (not moved by the topology)
                port = "%s#%s" % (adp["parent_mac"], adp["port"])
            else:
                port = "%s#%s" % (link.get("parent"), link.get("port")) if link.get("parent") and link.get("port") else None
            rows.append(dict(d, link_port=port))
        res = identity.groups(rows, rules["split"], rules["join"], self._identity_links())
        self._ident_cache = (key, res)
        return res

    def _identity_links(self) -> list:
        """Announced "these addresses are one device" facts: an LLDP neighbour's chassis and port addresses, and the
        several hardware addresses of one device in a hub's device registry."""
        out = []
        for entry in self.topo_obs.values():
            for n in ((entry or {}).get("ev") or {}).get("lldp") or []:
                macs = [m for m in (n.get("chassis_mac"), n.get("port_mac")) if m]
                ips = [i for i in (n.get("mgmt_ip") or n.get("chassis_ip"),) if i]
                if len(macs) + len(ips) >= 2:
                    out.append({"macs": macs, "ips": ips, "why": "same_lldp_chassis", "conf": 0.85})
        for e in self.hub.values():
            for d in ((e or {}).get("snapshot") or {}).get("devices") or []:
                macs = [m for m in d.get("macs") or [] if m]
                if 2 <= len(macs) <= 8:
                    out.append({"macs": macs, "ips": [], "why": "same_hub_device", "conf": 0.8})
        return out[:2000]

    def set_identity(self, device_id: str, action: str, other: str) -> dict:
        """split: show two devices separately for good; join: treat them as one; reset: forget both choices."""
        if action not in ("split", "join", "reset") or not isinstance(other, str) or other == device_id:
            raise ValueError("device.identity.invalid")
        if not self.store.device(device_id) or not self.store.device(other):
            raise ValueError("device.err.missing")
        pair = sorted([device_id, other])
        rules = self.identity_rules()
        rules["split"] = [p for p in rules["split"] if sorted(p) != pair]
        rules["join"] = [p for p in rules["join"] if sorted(p) != pair]
        if action != "reset":
            rules[action].append(pair)
        rules["split"], rules["join"] = rules["split"][-500:], rules["join"][-500:]
        self.store.set_setting("identity", rules)
        self._ident_cache = None
        self.structure_version += 1
        return self.device_view(self.store.device(device_id))

    def is_merged(self, device_id: str) -> bool:
        """A secondary interface of another device (not drawn on its own)."""
        return device_id in self.identity_groups()["members"]

    def _identity_view(self, dev: dict) -> dict:
        reg = self.registry()
        fp = identity.fingerprint(dict(dev["data"], mac=dev.get("mac")), reg)
        groups = self.identity_groups()
        did = dev["id"]
        out = {"vendor": fp["vendor"], "vendor_source": fp["vendor_source"], "model": fp["model"],
               "model_source": fp["model_source"], "firmware": fp["firmware"], "firmware_source": fp["firmware_source"],
               "origin": fp["origin"], "identity_confidence": fp["confidence"], "identity_reasons": fp["reasons"],
               "features": fp["features"], "ipv6": list(dev["data"].get("ipv6") or [])[:8],
               "netbios": dev["data"].get("netbios") or None, "merged_into": None, "aliases": [], "suggestions": []}
        mem = groups["members"].get(did)
        if mem:
            prim = self.store.device(mem["primary"])
            out["merged_into"] = {"id": mem["primary"], "title": (prim and (prim.get("user_title") or prim["title"])) or "",
                                  "ip": prim["ip"] if prim else "", "confidence": mem["confidence"], "reasons": mem["reasons"],
                                  "manual": mem["manual"]}
        for sid in groups["aliases"].get(did, []):
            sd, m = self.store.device(sid), groups["members"].get(sid) or {}
            if sd:
                out["aliases"].append({"id": sid, "ip": sd["ip"], "mac": sd.get("mac"), "ipv6": list(sd["data"].get("ipv6") or [])[:4],
                                       "confidence": m.get("confidence"), "reasons": m.get("reasons") or [],
                                       "manual": bool(m.get("manual")), "first_seen": sd["first_seen"]})
        for sg in groups["suggestions"]:
            if did in (sg["a"], sg["b"]):
                oid = sg["b"] if sg["a"] == did else sg["a"]
                od = self.store.device(oid)
                if od:
                    out["suggestions"].append({"id": oid, "title": od.get("user_title") or od["title"] or "", "ip": od["ip"],
                                               "confidence": sg["confidence"], "reasons": sg["reasons"]})
        return out

    def device_view(self, dev: dict, last_scan=False) -> dict:
        view = self._static_view(dev)
        view["status"] = self.device_status(dev, last_scan)
        if view["kind"] == "unknown" and self.hub:
            self._apply_hub_kinds([view], self.hub_merge())
        return self._with_hint(view)

    def _apply_hub_kinds(self, views: list, merge: Optional[dict] = None) -> None:
        """A device nobody could classify (no scan evidence, no choice of the user) that a hub identifies by its
        hardware address with enough confidence takes the class of that hub device (see hubmerge.kind_of); the
        reason says so. A choice of the user and a class the scan found always win."""
        if not self.hub or not any(v["kind"] == "unknown" and not v.get("merged_into") for v in views):
            return
        merge = merge if merge is not None else self.hub_merge(views)
        snaps = {i: (e.get("snapshot") or {}) for i, e in self.hub.items()}
        for v in views:
            if v["kind"] != "unknown" or v.get("merged_into"):
                continue
            for m in merge["by_net"].get(v["id"]) or []:
                hd = next((d for d in snaps.get(m["integ"], {}).get("devices") or [] if d.get("id") == m["device_id"]), None)
                kind = hubmerge.kind_of(hd) if hd else None
                if kind:
                    v["kind"] = kind
                    v["kind_source"] = "hub"
                    v["reasons"] = list(v.get("reasons") or []) + ["hub:" + m["how"]]
                    break

    def _with_hint(self, view: dict) -> dict:
        ai = getattr(self, "ai", None)
        if ai is not None and getattr(ai, "hints", None) is not None:
            from .ai.features import apply_hint
            view = apply_hint(view, ai.hints.peek(view["id"]))     # a confirmed AI hint: weak evidence only
        return view

    def _static_view(self, dev: dict) -> dict:
        data = dev["data"]
        kind = dev.get("user_kind") or dev["kind"]
        title = dev.get("user_title") or dev["title"] or ""
        view = {
            "id": dev["id"], "ip": dev["ip"], "mac": dev.get("mac"), "kind": kind, "auto_kind": dev["kind"],
            "title": title, "auto_title": dev["title"], "confidence": dev["confidence"],
            "certainty": data.get("certainty", "unknown"), "reasons": data.get("reasons", []),
            "hostnames": data.get("hostnames", []), "services": classify.services_of(data),
            "ports": data.get("ports", []), "mdns": data.get("mdns", []), "ssdp": data.get("ssdp", []),
            "sources": data.get("sources", []), "is_gateway": bool(data.get("is_gateway")), "is_self": bool(data.get("is_self")),
            "hidden": bool(dev["hidden"]), "first_seen": dev["first_seen"], "last_seen": dev["last_seen"],
            "status": "unknown",
        }
        view.update(self._identity_view(dev))
        view["protect"] = self._protect_view(dev)
        view["adapter"] = data.get("adapter") if isinstance(data.get("adapter"), dict) else None
        view["adapter_only"] = adapter_inventory.is_adapter_only(data)
        view["zone"] = None
        return view

    # ------------------------------------------------------------------ protection list
    def protect_marks(self) -> dict:
        marks = self.store.get_setting("protect_marks", {}) or {}
        return {k: v for k, v in marks.items() if isinstance(k, str) and v in protect_mod.MARKS}

    def _protect_view(self, dev: dict):
        marks = self.protect_marks()
        mark = marks.get(dev["id"])
        klass, why, source = protect_mod.resolve(dev["data"], mark)
        auto, auto_why = protect_mod.auto_class(dev["data"])
        if klass is None and mark is None and auto is None:
            return None
        return {"class": klass, "why": why, "source": source, "mark": mark, "auto": auto}

    def set_protect(self, device_id: str, mark) -> dict:
        """The user's mark for one device: printer, industrial, passive, none (no automatic class) or None (forget)."""
        if mark is not None and mark not in protect_mod.MARKS:
            raise ValueError("protect.err.invalid")
        if not self.store.device(device_id):
            raise ValueError("device.err.missing")
        marks = self.protect_marks()
        if mark is None:
            marks.pop(device_id, None)
        else:
            marks[device_id] = mark
        self.store.set_setting("protect_marks", dict(list(marks.items())[-2000:]))
        self.structure_version += 1
        return self.device_view(self.store.device(device_id))

    def protected_map(self) -> dict:
        """{ip: {"class", "why", "source"}} for the scanner: what the inventory already knows about sensitive devices."""
        marks, out = self.protect_marks(), {}
        for d in self.store.devices():
            if not d["ip"]:
                continue
            klass, why, source = protect_mod.resolve(d["data"], marks.get(d["id"]))
            if klass:
                out[d["ip"]] = {"class": klass, "why": why, "source": source}
            elif marks.get(d["id"]) == "none":
                out[d["ip"]] = {"source": "none"}
        return out

    def protect_list(self) -> list:
        """Every device with a protection class, for the list in Settings (most protective first)."""
        out = []
        for d in self.devices():
            p = d.get("protect")
            if p and p.get("class"):
                out.append({"id": d["id"], "title": d["title"], "ip": d["ip"], "mac": d["mac"], "kind": d["kind"],
                            "class": p["class"], "why": p["why"], "source": p["source"]})
        rank = {"passive": 0, "industrial": 1, "printer": 2}
        return sorted(out, key=lambda e: (rank.get(e["class"], 3), e["title"].lower(), e["id"]))

    def device_status(self, dev: dict, last_scan=False) -> str:
        now = time.time()
        last = self._last_scan_id() if last_scan is False else last_scan
        alive = max(self.presence.get(dev["id"], 0), dev["last_seen"] if dev.get("last_scan") == last else 0)
        if self.flow_seen.get(dev["id"], 0) > now - 30:
            return "up"
        if alive and now - alive < PRESENCE_INTERVAL * 3:
            return "up"
        if dev.get("last_scan") is not None and dev.get("last_scan") != last:
            return "down"
        return "unknown" if not alive else "down"

    def map_change(self, now: float) -> int:
        """Change channel for the map: a number that rises when device statuses, flow traces or source states change.

        Checked at most every 5 s from rows the database already has (no map build); the browser fetches the map only
        when this number or the structure version moves, instead of polling it."""
        if now - self._map_sig_t < 5.0:
            return self.map_version
        self._map_sig_t = now
        last = self._last_scan_id()
        parts = []
        for r in self.store.query("SELECT id, last_seen, last_scan, hidden FROM devices ORDER BY id"):
            dev_id = r["id"]
            alive = max(self.presence.get(dev_id, 0), r["last_seen"] if r["last_scan"] == last else 0)
            if self.flow_seen.get(dev_id, 0) > now - 30 or (alive and now - alive < PRESENCE_INTERVAL * 3):
                st = "u"
            elif r["last_scan"] is not None and r["last_scan"] != last:
                st = "d"
            else:
                st = "n" if not alive else "d"
            flow = "f" if now - self.flow_seen.get(dev_id, 0) < FLOW_KEEP else ""
            parts.append("%s%s%s%d" % (dev_id, st, flow, r["hidden"]))
        parts.extend("%s%d" % (k, 1 if v.get("ok") else 0) for k, v in sorted(self.source_status.items()))
        agents = getattr(self, "agents", None)
        sig = hash((self.structure_version, last, tuple(parts), agents.version if agents else 0))
        if sig != self._map_sig:
            if self._map_sig is not None:
                self.map_version += 1
            self._map_sig = sig
        return self.map_version

    def _last_scan_id(self):
        row = self.store.one("SELECT id FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1")
        return row["id"] if row else None

    def patch_device(self, device_id: str, patch: dict) -> Optional[dict]:
        dev = self.store.device(device_id)
        if not dev:
            return None
        if "title" in patch:
            title = (patch.get("title") or "").strip()[:48] or None
            self.store.execute("UPDATE devices SET user_title=? WHERE id=?", (title, device_id))
        if "kind" in patch:
            kind = patch.get("kind") or None
            if kind is not None and kind not in classify.KINDS:
                kind = None
            self.store.execute("UPDATE devices SET user_kind=? WHERE id=?", (kind, device_id))
        if "hidden" in patch:
            self.store.execute("UPDATE devices SET hidden=? WHERE id=?", (1 if patch.get("hidden") else 0, device_id))
        if "link" in patch:
            self.set_manual_link(device_id, patch.get("link"))     # raises ValueError(reason key)
        self._refresh_index()
        return self.device_view(self.store.device(device_id))

    def forget_device(self, device_id: str) -> bool:
        ok = self.store.execute("DELETE FROM devices WHERE id=?", (device_id,)).rowcount > 0
        if ok:
            self.paths.forget_device(device_id)
            self.availability.forget("dev:" + device_id)
            self.services.forget_device(device_id)
            self.hosthealth.forget(device_id)
        self.store.execute("DELETE FROM phys_manual WHERE device_id=? OR parent_id=?", (device_id, device_id))
        self.inventory.forget(device_id)
        self.vulns.forget_device(device_id)
        self._manual_ver += 1
        self._refresh_index()
        return ok

    # ------------------------------------------------------------------ physical topology
    def paths_summary(self, window: str) -> dict:
        """Data paths of one window (24h, 7d, 30d) with device titles, for the data paths view."""
        devices = self._paths_devices()
        out = dict(self.paths.summary(window, devices))
        out["source"] = self.paths_source()
        out["routes"] = self.with_cables(self.routes.rows(limit=20)) if self.routes.settings()["enabled"] or self.routes.demo else []
        out["routes_on"] = bool(self.routes.settings()["enabled"])
        out["infra_stand"] = self.paths_infra_stand()
        return out

    def _paths_devices(self) -> dict:
        devices = {}
        for dev in self.store.devices():
            data = dev["data"]
            title = "" if data.get("is_self") else (dev.get("user_title") or dev["title"] or "")
            devices[dev["id"]] = {"title": title, "ip": dev["ip"], "kind": dev.get("user_kind") or dev["kind"],
                                  "self": bool(data.get("is_self"))}
        devices.setdefault("self", {"title": "", "ip": self.self_ip, "kind": "server", "self": True})
        devices.setdefault(netflow.UNKNOWN_DEVICE, {"title": "", "ip": None, "kind": "", "self": False})
        return devices

    def paths_lan(self, window: str) -> dict:
        """Conversations between two devices of the LAN (from gateway flows), for the map's "Who talks to whom"."""
        devices = self._paths_devices()
        rows = [r for r in self.paths.pairs(window) if r["a"] in devices and r["b"] in devices][:datapaths.MAX_PAIRS]
        return {"window": window if window in datapaths.PERIODS else "24h", "version": self.paths.version, "pairs": rows,
                "source": self.paths_source()}

    def paths_country(self, cc: str, window: str) -> dict:
        """The detail sheet of one country (cities and operators only with the option on), with device titles."""
        rw = self.routes
        on = bool(rw.settings()["enabled"])
        cc_n = (cc or "").strip().upper()[:2]
        out = dict(self.paths.country_view(cc, window, self._paths_devices(), route_places=rw.places_for(cc_n) if on else None))
        out["source"] = self.paths_source()
        out["routes"] = self.with_cables(rw.rows(cc_n)) if (on or rw.demo) else []
        out["routes_on"] = on
        out["infra_stand"] = self.paths_infra_stand()
        out["home_cc"] = (self.settings().get("home_country") or "").upper()
        return out

    def paths_routes(self) -> dict:
        """Network routes (DW2): state of the option, what is possible here, and the stored (condensed) routes."""
        rw = self.routes
        rows = self.with_cables(rw.rows(limit=40)) if (rw.settings()["enabled"] or rw.demo) else []
        return {"status": rw.status(), "routes": rows, "infra_stand": self._infra_stand(rows)}

    def _infra_inputs(self) -> tuple:
        """(cable data, home, stored routes with their cable estimates) for the infrastructure routes - the same rows as the routes view."""
        rw = self.routes
        rows = self.with_cables(rw.rows(limit=40)) if (rw.settings()["enabled"] or rw.demo) else []
        cc = (self.settings().get("home_country") or "").strip().upper()[:2]
        return self.cables.data(), ({"cc": cc} if cc else None), rows

    def _infra_stand(self, rows: list) -> Optional[str]:
        """Fingerprint of the infrastructure routes (cable data, home country, stored routes) - cheap, nothing is computed."""
        from . import infraroute
        cc = (self.settings().get("home_country") or "").strip().upper()[:2]
        if not cc:
            return None
        return infraroute.InfraRoutes.stand(self.cables.data(), {"cc": cc}, rows, self.cables.version)

    def paths_infra_stand(self) -> Optional[str]:
        data, home, rows = self._infra_inputs()
        return self._infra_stand(rows) if home else None

    def paths_infra(self) -> Optional[dict]:
        """Infrastructure routes (view "Real cables"): for every country of the world map and every stored route the way over
        land routes, cables, radio or satellite (``infraroute.py``). Kept in memory per cable version, home country and route
        state; works without cable data and without measured routes. None while no home country is known."""
        from . import infraroute
        data, home, rows = self._infra_inputs()
        if not home:
            return None
        if self._infra is None:
            world = infraroute.load_world(Path(__file__).resolve().parent / "web" / "js" / "worldmap.js")
            self._infra = infraroute.InfraRoutes(infraroute.country_points(world), world)
        try:
            return self._infra.compute(data, home, rows, version=self.cables.version)
        except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
            return None

    def with_cables(self, rows: list) -> list:
        """Real cables (DW3): every route gets its sea crossings, the estimated cables (never sure) and the path over the
        cables for the map. Computed from the stored sections (no address in them) and kept in memory only - nothing of it
        is stored. Without routes the cable file is not even opened."""
        if not rows:
            return rows
        from . import cableroute, infraroute
        data = self.cables.data()
        ver = self.cables.version
        places = self.routes.places if data else {}
        if len(self._cable_cache) > 400:
            self._cable_cache.clear()
        out = []
        for r in rows:
            k = (r.get("key"), r.get("t"), ver)
            hit = self._cable_cache.get(k)
            if hit is None:
                secs = r.get("sections") or []
                try:
                    est = cableroute.estimate(secs, data, places=places) if data else []
                    hit = {"cables": [{x: e[x] for x in e if x not in ("line", "ends")} for e in est],
                           "crossings": cableroute.crossings(secs), "path": cableroute.path(secs, est)}
                except (TypeError, ValueError, KeyError, IndexError, ZeroDivisionError):
                    hit = {"cables": [], "crossings": [], "path": []}
                self._cable_cache[k] = hit
            row = dict(r, **hit)
            sat = infraroute.satellite(r.get("asn"), r.get("operator"))
            if sat:
                row["sat"] = sat                                   # the destination network is a satellite network (leo/geo)
            out.append(row)
        return out

    def paths_cables_action(self, action: str, raw: bytes = b"") -> dict:
        """``import`` (administrator: an Overpass answer of their own replaces the bundled cables), ``reset`` (back to the
        bundled file). Raises ValueError with a translation key."""
        if action == "import":
            out = self.cables.import_answer(raw, time.strftime("%Y-%m-%d", time.gmtime(time.time())))
        elif action == "reset":
            out = self.cables.reset()
        else:
            raise ValueError("paths.cables.action")
        self._cable_cache.clear()
        return out

    def paths_routes_action(self, action: str) -> dict:
        """``measure`` (operator: measure the top destinations now), ``clear`` (administrator: forget every stored route)."""
        if action == "measure":
            return self.routes.measure_now()
        if action == "clear":
            self.routes.clear()
            return self.routes.status()
        raise ValueError("paths.routes.action")

    def paths_detail_action(self, action: str) -> dict:
        """Administrator actions on the city data: ``refresh`` (check now), ``clear`` (delete files and stored figures)."""
        city = self.geo.city
        if action == "refresh":
            if city.refresh_now():
                threading.Thread(target=city.step, name="lanloom-geocity-now", daemon=True).start()
        elif action == "clear":
            city.configure(False)
            self._setting_store("paths_detail").set_setting("paths_detail", False)
            city.clear()
            self.paths.clear_detail()
        else:
            raise ValueError("paths.detail.action")
        return self.geo.info()["detail"]

    def paths_device(self, device: str, window: str) -> dict:
        """Where one device (or the internet tile) talks to: countries and LAN partners, with titles."""
        out = self.paths.device_view(device, window, self._paths_devices())
        out["source"] = self.paths_source()
        return out

    def manual_links(self) -> dict:
        return {r["device_id"]: {"parent": r["parent_id"], "port": r["port"], "medium": r["medium"], "t": r["updated"]}
                for r in self.store.query("SELECT * FROM phys_manual")}

    def topology(self) -> dict:
        """Resolved physical links (manual > controller > LLDP > forwarding database), cached until something changes."""
        key = (self.structure_version, self._manual_ver, int(time.time() // 30))
        if self._topo_cache and self._topo_cache[0] == key:
            return self._topo_cache[1]
        devices = [self.device_view(d) for d in self.store.devices()]
        topo = topology.resolve(devices, self.manual_links(), self.topo_obs)
        self._topo_cache = (key, topo)
        return topo

    def set_manual_link(self, device_id: str, link) -> None:
        """Manual "connected via": {parent, port, medium} or None to remove. Raises ValueError(reason key)."""
        if not link:
            self.store.execute("DELETE FROM phys_manual WHERE device_id=?", (device_id,))
        else:
            if not isinstance(link, dict):
                raise ValueError("topo.err.invalid")
            parent = str(link.get("parent") or "").strip()
            medium = link.get("medium") or "wired"
            if medium not in topology.MEDIA:
                raise ValueError("topo.err.invalid")
            port = topology.clean_port(link.get("port")) if medium != "virtual" else None
            topo = self.topology()
            known = bool(self.store.device(parent)) or parent in topo["nodes"] or parent == "internet"
            if not parent or parent == device_id or not known or parent == "internet":
                raise ValueError("topo.err.parent")
            # a device cannot hang below one of its own descendants (that would close a loop)
            manual = self.manual_links()
            manual[device_id] = {"parent": parent}
            node, guard = parent, 0
            while node and guard < 10000:
                if node == device_id:
                    raise ValueError("topo.err.loop")
                node, guard = (manual.get(node) or topo["links"].get(node) or {}).get("parent"), guard + 1
            self.store.execute("INSERT OR REPLACE INTO phys_manual(device_id, parent_id, port, medium, updated) VALUES(?,?,?,?,?)",
                               (device_id, parent, port, medium, time.time()))
        self._manual_ver += 1
        self._topo_cache = None
        self._topo_sig = self.topology()["signature"]
        self.structure_version += 1

    # ------------------------------------------------------------------ scanning
    NETWORKS_TTL = 30.0

    def networks(self) -> list:
        """Local networks of this host; read again at most every 30 s (the network view asks on every build)."""
        if self.demo:
            return self.demo.networks()
        if self.remote:
            return []                     # the interfaces of this host belong to the home site
        now = time.time()
        cached = getattr(self, "_nets_cache", None)
        if cached and 0 <= now - cached[0] < self.NETWORKS_TTL:
            return [dict(n) for n in cached[1]]
        from . import scan
        try:
            nets = scan.detect_networks()
        except Exception:     # never let interface detection break the page
            log.exception("network detection failed")
            return []
        self._nets_cache = (now, nets)
        return [dict(n) for n in nets]

    def start_scan(self, cidrs: list, deep: bool = False, consent: bool = False, automatic: bool = False,
                   snmp_public: Optional[bool] = None) -> dict:
        """``snmp_public``: ask devices with the default read community (off unless the user switched it on; the
        choice is kept for automatic rescans)."""
        if self.cfg.probe:
            raise ValueError("app.err.probe")     # a trial start never sends a packet into the LAN
        if self._scan_task and not self._scan_task.done():
            raise ValueError("scan.err.running")
        if not cidrs:
            raise ValueError("scan.err.no_network")
        if self.remote:
            # a remote site is behind other routers: only ranges the user explicitly agreed to (Networks and zones)
            agreed = set(self.zone_consent())
            from .scan import safety
            for c in cidrs:
                net = safety.parse_cidr(c)
                if net is None or str(net) not in agreed:
                    raise ValueError("site.err.scan_consent")
        if self.demo:
            plan = {"cidrs": list(cidrs), "deep": bool(deep)}
        else:
            from . import scan
            if snmp_public is None:
                snmp_public = self.store.get_setting("scan_snmp_public", False) is True
            plan = scan.make_plan(list(cidrs), deep=bool(deep), consent=bool(consent),
                                  snmp_public=bool(snmp_public))   # raises ValueError(reason_key)
        mode = "deep" if deep else "gentle"
        scan_id = self.store.execute(
            "INSERT INTO scans(started, mode, cidrs, status) VALUES(?,?,?,?)",
            (time.time(), mode, json.dumps(list(cidrs)), "running")).lastrowid
        self.scan_state = {"state": "running", "id": scan_id, "mode": mode, "cidrs": list(cidrs), "started": time.time(),
                           "phase": "neighbors", "done": 0, "total": 0, "found": 0, "automatic": automatic}
        self._scan_task = asyncio.create_task(self._run_scan(scan_id, plan))
        if not automatic:
            self.store.set_setting("rescan_cidrs", list(cidrs))
            if snmp_public is not None:
                self.store.set_setting("scan_snmp_public", bool(snmp_public))
        return dict(self.scan_state)

    def cancel_scan(self) -> bool:
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            return True
        return False

    async def _run_scan(self, scan_id: int, plan: dict) -> None:
        def progress(p: dict) -> None:
            for k in ("phase", "done", "total", "found"):
                if k in p:
                    self.scan_state[k] = p[k]

        try:
            if self.demo:
                result = await self.demo.scan(plan, progress, preview=lambda r: self._preview(r, scan_id))
            else:
                from . import scan
                result = await scan.run(plan, progress, preview=lambda r: self._preview(r, scan_id),
                                        protected=self.protected_map())
            count = self.ingest(result, scan_id)
            self._mark_first_picture()
            self.store.execute("UPDATE scans SET finished=?, status='done', hosts=?, stats=? WHERE id=?",
                               (time.time(), count, json.dumps(result.get("stats") or {}), scan_id))
            self.scan_state.update({"state": "done", "finished": time.time(), "found": count})
        except asyncio.CancelledError:
            self.store.execute("UPDATE scans SET finished=?, status='cancelled' WHERE id=?", (time.time(), scan_id))
            self.scan_state.update({"state": "cancelled", "finished": time.time()})
        except ValueError as exc:
            self.store.execute("UPDATE scans SET finished=?, status='failed' WHERE id=?", (time.time(), scan_id))
            self.scan_state.update({"state": "failed", "finished": time.time(), "error": str(exc)})
        except Exception:
            log.exception("scan failed")
            self.store.execute("UPDATE scans SET finished=?, status='failed' WHERE id=?", (time.time(), scan_id))
            self.scan_state.update({"state": "failed", "finished": time.time(), "error": "scan.err.internal"})
        finally:
            self.cadence.setdefault("scan", live.Cadence()).tick()

    def _preview(self, result: dict, scan_id: int) -> int:
        """First picture of a running scan: adds the hosts found by the quick phases. Only devices that are not in the
        inventory yet - a rescan never replaces richer data with the preliminary view; the full result follows."""
        fresh = [h for h in (result.get("hosts") or [])
                 if h.get("ip") and not self.store.one("SELECT id FROM devices WHERE id=?", (classify.device_id(h),))]
        n = self.ingest({"hosts": fresh}, scan_id) if fresh else 0
        if self.scan_state.get("id") == scan_id:
            self.scan_state["preview"] = len(result.get("hosts") or [])
            self.scan_state["preview_at"] = time.time()
        self._mark_first_picture()
        return n

    def _mark_first_picture(self) -> None:
        """Seconds from the start of the setup to the first picture on the map (kept once, shown at the end of the
        setup and measured by the test bench)."""
        if self.store.get_setting("setup_first_picture_s", None) is not None:
            return
        t0 = self.store.get_setting("setup_started", None)
        if not isinstance(t0, (int, float)) or not self.store.devices():
            return
        self.store.set_setting("setup_first_picture_s", round(max(0.0, time.time() - t0), 1))

    def ingest(self, result: dict, scan_id: Optional[int]) -> int:
        """Classify every host of a ScanResult and store it; returns the number of hosts. Each host is stored on its
        own savepoint: a host that cannot be stored is logged and skipped, it never costs the rest of the scan."""
        hosts = result.get("hosts") or []
        moved = []
        had_self = self.self_device
        with self.store.transaction():
            for host in hosts:
                self.store.execute("SAVEPOINT ingest_host")
                try:
                    change = self._ingest_host(host, scan_id)
                except Exception:
                    self.store.execute("ROLLBACK TO ingest_host")
                    log.exception("could not store a scanned host, skipped (the rest of the scan is kept)")
                    change = None
                self.store.execute("RELEASE ingest_host")
                if change:
                    moved.append(change)
        # the index first: from here on flows are booked on the new ids; what was booked on an old id meanwhile is still
        # pending and moves along below (lanloom#13: this host's flows stayed on its address id after it got its hardware id)
        self._refresh_index()
        for old_id, new_id, merged in moved:       # outside the transaction: these take their own locks
            self.paths.rename_device(old_id, new_id)
            if merged:
                self.availability.forget("dev:" + old_id)
                self.hosthealth.forget(old_id)
                self.vulns.forget_device(old_id)
        if self.self_device and self.self_device != had_self:
            self.paths.rename_device("self", self.self_device)    # booked before this host had an entry
        return len(hosts)

    def _ingest_host(self, host: dict, scan_id: Optional[int]) -> Optional[tuple]:
        """Store one scanned host. Returns (old id, new id, merged) when an address-only row took the id of its hardware
        address - renamed, or merged into a row that had it already (the data paths of the old id move to the new one after
        the transaction, other leftovers of a merged row are dropped) - else None."""
        if not host.get("ip") and classify.device_id(host).startswith("d-ip-"):
            return None               # neither an address nor a hardware address: nothing to identify it by
        verdict = classify.classify(host, vendor=self.registry().lookup(host.get("mac")))
        data = dict(host)
        data.update({"certainty": verdict["certainty"], "reasons": verdict["reasons"],
                     "runner_up": verdict["runner_up"]})
        dev_id = classify.device_id(host)
        if "adapter" not in data:     # what an adapter told about the device stays until the next booking
            old_row = self.store.one("SELECT data FROM devices WHERE id=?", (dev_id,))
            old_adapter = json.loads(old_row["data"]).get("adapter") if old_row else None
            if old_adapter:
                data["adapter"] = old_adapter
        gone = change = None
        # a device first seen by IP and now with a MAC keeps its user edits
        if dev_id.startswith("d-") and not dev_id.startswith("d-ip-") and host.get("ip"):
            old = self.store.one("SELECT id, user_title, user_kind, first_seen FROM devices WHERE id=?",
                                 ("d-ip-" + host["ip"].replace(".", "-").replace(":", "-"),))
            if old:
                old_id = old["id"]
                if self.store.one("SELECT id FROM devices WHERE id=?", (dev_id,)):
                    # the hardware address is known already (the neighbour entry was missing in an earlier scan):
                    # merge - the row with the hardware address stays, the address row only fills its gaps. Hidden
                    # is not carried over: a device that answers does not vanish because an old leftover was hidden.
                    self.store.execute(
                        "UPDATE devices SET user_title=CASE WHEN COALESCE(user_title, '')='' THEN ? ELSE user_title END,"
                        " user_kind=CASE WHEN COALESCE(user_kind, '')='' THEN ? ELSE user_kind END,"
                        " first_seen=MIN(first_seen, ?) WHERE id=?",
                        (old["user_title"], old["user_kind"], old["first_seen"], dev_id))
                    self.store.execute("DELETE FROM devices WHERE id=?", (old_id,))
                    gone = old_id
                else:
                    self.store.execute("UPDATE devices SET id=? WHERE id=?", (dev_id, old_id))
                # a manual link of the device itself: the one of the hardware-address row wins
                self.store.execute("UPDATE OR IGNORE phys_manual SET device_id=? WHERE device_id=?", (dev_id, old_id))
                self.store.execute("DELETE FROM phys_manual WHERE device_id=?", (old_id,))
                self.store.execute("UPDATE phys_manual SET parent_id=? WHERE parent_id=?", (dev_id, old_id))
                self.store.execute("DELETE FROM phys_manual WHERE device_id=parent_id")
                self.inventory.rename_device(old_id, dev_id)
                if gone:
                    self.inventory.forget(old_id)       # entries the hardware-address row had already win
                self._manual_ver += 1
                change = (old_id, dev_id, bool(gone))
        self.store.upsert_device({"id": dev_id, "ip": host.get("ip") or "", "mac": host.get("mac"), "data": data,
                                  "kind": verdict["kind"], "confidence": verdict["confidence"],
                                  "title": verdict["title"]}, scan_id)
        self.presence[dev_id] = time.time()
        return change

    async def _rescan_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                hours = int(self.store.get_setting("rescan_hours", 6) or 0)
                cidrs = list(self.store.get_setting("rescan_cidrs", []) or [])
                cidrs += [c for c in self.zone_consent() if c not in cidrs]      # ranges the user allowed across a router
                cidrs = cidrs[:8]
                if not hours or not cidrs or (self._scan_task and not self._scan_task.done()):
                    continue
                last = self.store.one("SELECT started FROM scans ORDER BY id DESC LIMIT 1")
                if last and time.time() - last["started"] < hours * 3600:
                    continue
                self.start_scan(cidrs, deep=False, consent=False, automatic=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("automatic rescan failed")

    async def _presence_loop(self) -> None:
        """Gentle liveness check: one TCP connect to a known open port, else the kernel neighbour table."""
        while True:
            await asyncio.sleep(15)
            try:
                await self._presence_round()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("presence check failed")
            self.cadence.setdefault("presence", live.Cadence(PRESENCE_INTERVAL)).tick()
            await asyncio.sleep(PRESENCE_INTERVAL - 15)

    async def _presence_round(self) -> None:
        neighbours = {}
        try:
            with open("/proc/net/arp", encoding="ascii") as fh:
                for line in fh.read().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 4 and parts[2] == "0x2":
                        neighbours[parts[0]] = True
        except OSError:
            pass
        sem = asyncio.Semaphore(8)

        marks = self.protect_marks()

        async def probe(dev: dict) -> None:
            klass = protect_mod.resolve(dev["data"], marks.get(dev["id"]))[0]
            ports = [p["port"] for p in dev["data"].get("ports") or [] if isinstance(p.get("port"), int)
                     and (klass is None or klass == "printer" and p["port"] not in protect_mod.PRINTER_RAW
                          or klass == "industrial" and p["port"] in protect_mod.INDUSTRIAL_WEB)]
            if dev["data"].get("is_self"):
                self.presence[dev["id"]] = time.time()
                return
            if ports:
                async with sem:
                    try:
                        _, writer = await asyncio.wait_for(asyncio.open_connection(dev["ip"], ports[0]), 0.8)
                        writer.close()
                        self.presence[dev["id"]] = time.time()
                        return
                    except ConnectionRefusedError:
                        self.presence[dev["id"]] = time.time()     # refused = host answered
                        return
                    except (OSError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(0.1)                         # at most ~80 probes per second overall
            if neighbours.get(dev["ip"]):
                self.presence[dev["id"]] = time.time()

        await asyncio.gather(*(probe(d) for d in self.store.devices(include_hidden=False) if d["ip"] and _is_private(d["ip"])))

    # ------------------------------------------------------------------ integrations
    def integrations(self) -> list:
        out = []
        for row in self.store.query("SELECT * FROM integrations ORDER BY id"):
            cfg = json.loads(row["config"])
            secrets_set = {f: bool(self.vault and self.vault.has("int:%d:%s" % (row["id"], f)))
                           for f in (integration_fields(row["kind"]) or ((), ()))[1]}
            st = self.source_status.get("int:%d" % row["id"], {})
            out.append({"id": row["id"], "kind": row["kind"], "name": row["name"], "config": cfg, "secrets": secrets_set,
                        "enabled": bool(row["enabled"]), "status": st,
                        "grade": self._source_grade("int:%d" % row["id"])})
        return out

    @staticmethod
    def clean_integration(kind: str, config: dict, secrets: dict) -> tuple:
        fields = integration_fields(kind)
        if fields is None:
            raise ValueError("integration.err.kind")
        plain_fields, secret_fields = fields
        cfg = {k: config.get(k) for k in plain_fields if config.get(k) not in (None, "")}
        sec = {k: str(secrets.get(k)) for k in secret_fields if secrets.get(k) not in (None, "")}
        if kind not in INTEGRATION_FIELDS:            # a built-in family: plain settings are short text or flags
            for k, v in list(cfg.items()):
                if isinstance(v, bool):
                    continue
                v = str(v).strip()
                if len(v) > 256 or any(ord(c) < 32 for c in v):
                    raise ValueError("integration.err.value")
                cfg[k] = v
            if "url" in plain_fields:
                parsed = urlparse(str(cfg.get("url") or ""))
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("integration.err.url")
                cfg["url"] = cfg["url"].rstrip("/")
        if kind in ("hub", "controller"):
            url = str(cfg.get("url") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("integration.err.url")
            cfg["url"] = url.rstrip("/")
        if kind == "controller":
            site = str(cfg.get("site") or "default").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", site):
                raise ValueError("integration.err.site")
            cfg["site"] = site
            if not str(cfg.get("username") or "").strip():
                raise ValueError("integration.err.username")
            cfg["username"] = str(cfg["username"]).strip()[:64]
        if kind in ("mqtt", "snmp", "nut"):
            host = str(cfg.get("host") or "").strip()
            if not host or len(host) > 253 or any(c.isspace() for c in host):
                raise ValueError("integration.err.host")
            cfg["host"] = host
        if kind == "mqtt":
            try:
                cfg["port"] = int(cfg.get("port") or (8883 if cfg.get("tls") else 1883))
            except (TypeError, ValueError):
                raise ValueError("integration.err.port")
            cfg["tls"] = bool(cfg.get("tls"))
        if kind == "snmp":
            cfg["version"] = "3" if str(cfg.get("version") or "2c") == "3" else "2c"
        if kind == "nut":
            try:
                cfg["port"] = int(cfg.get("port") or 3493)
            except (TypeError, ValueError):
                raise ValueError("integration.err.port")
            if not 1 <= cfg["port"] <= 65535:
                raise ValueError("integration.err.port")
            cfg["ups"] = str(cfg.get("ups") or "").strip()
            if cfg["ups"] and not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", cfg["ups"]):
                raise ValueError("integration.err.value")
            if not cfg["ups"]:
                cfg.pop("ups", None)
        for k in ("allow_self_signed", "tls", "admin_mode") + tuple(hub_privacy.GROUP_FIELDS):
            if k in cfg:
                cfg[k] = cfg[k] is True
        return cfg, sec

    def _build_source(self, kind: str, cfg: dict, sec: dict):
        from . import sources
        if kind == "hub":
            return sources.Hub(cfg["url"], sec.get("token", ""), allow_self_signed=bool(cfg.get("allow_self_signed")),
                               admin_mode=bool(cfg.get("admin_mode")), show=hub_privacy.shown_domains(cfg))
        if kind == "mqtt":
            return sources.Mqtt(cfg["host"], port=cfg.get("port", 1883), username=cfg.get("username"),
                                password=sec.get("password"), tls=bool(cfg.get("tls")),
                                allow_self_signed=bool(cfg.get("allow_self_signed")), sys_topics=True)
        if kind == "snmp":
            return sources.Snmp(cfg["host"], version=cfg.get("version", "2c"), community=sec.get("community"),
                                user=cfg.get("user"), auth_key=sec.get("auth_key"), priv_key=sec.get("priv_key"),
                                auth_proto=cfg.get("auth_proto") or "sha", priv_proto=cfg.get("priv_proto") or "aes")
        if kind == "nut":
            return sources.Nut(cfg["host"], port=cfg.get("port", 3493), ups=cfg.get("ups") or "")
        if kind == "controller":
            return sources.Controller(cfg["url"], cfg.get("username") or "", sec.get("password") or "", site=cfg.get("site") or "default",
                                      style=cfg.get("style") if cfg.get("style") in ("console", "classic") else "auto",
                                      allow_self_signed=bool(cfg.get("allow_self_signed")))
        if integration_fields(kind) is not None:       # read half of a built-in family; credentials wrapped
            from . import integrations
            from .integrations.credentials import Secret
            return integrations.load_read(kind, **cfg, **{k: Secret(v) for k, v in sec.items()})
        raise ValueError("integration.err.kind")

    @staticmethod
    async def _scope_refused(src) -> bool:
        """A read half that can tell its account can write is refused (read-only accounts only)."""
        from .integrations.credentials import refuse_write_capable
        check = getattr(src, "account_scope", None)
        if check is None:
            return False
        try:
            return refuse_write_capable(await asyncio.wait_for(check(), 25))
        except Exception:
            return False

    async def test_integration(self, kind: str, config: dict, secrets: dict, existing_id: Optional[int] = None) -> dict:
        cfg, sec = self.clean_integration(kind, config, secrets)
        if existing_id and self.vault:
            for field in integration_fields(kind)[1]:
                if field not in sec:
                    stored = self.vault.get("int:%d:%s" % (existing_id, field))
                    if stored:
                        sec[field] = stored
        if self.demo:
            module = "hostnet" if kind == "host" else kind if kind in INTEGRATION_FIELDS else "family"
            return {"ok": True, "key": "source.%s.ok" % module, "details": self.demo.test_details(kind)}
        try:
            src = self._build_source(kind, cfg, sec)
            if await self._scope_refused(src):
                return {"ok": False, "key": "integration.err.can_write", "details": {}}
            ok, key, details = await asyncio.wait_for(src.test(), 25)
        except asyncio.TimeoutError:
            ok, key, details = False, "source.%s.timeout" % (kind if kind in INTEGRATION_FIELDS else "family"), {}
        except Exception as exc:     # sources raise SourceError with a reason key
            ok, key, details = False, getattr(exc, "key", "source.%s.error" % (kind if kind in INTEGRATION_FIELDS else "family")), {}
        return {"ok": bool(ok), "key": key, "details": details or {}}

    def save_integration(self, kind: str, name: str, config: dict, secrets: dict, existing_id: Optional[int] = None) -> dict:
        cfg, sec = self.clean_integration(kind, config, secrets)
        name = (name or "").strip()[:48] or kind
        if existing_id:
            if not self.store.one("SELECT id FROM integrations WHERE id=?", (existing_id,)):
                raise ValueError("integration.err.missing")
            self.store.execute("UPDATE integrations SET name=?, config=? WHERE id=?", (name, json.dumps(cfg), existing_id))
            integ_id = existing_id
        else:
            integ_id = self.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                                          (kind, name, json.dumps(cfg), time.time())).lastrowid
        if self.vault:
            for field, value in sec.items():
                self.vault.put("int:%d:%s" % (integ_id, field), value)
        self._stop_source("int:%d" % integ_id)
        integ = [i for i in self.integrations() if i["id"] == integ_id][0]
        if not self.demo and not self.cfg.probe:
            self._start_integration(integ)
        self.structure_version += 1
        return integ

    def integration_parts(self, integ_id: int) -> Optional[tuple]:
        """(kind, plain config, secrets) of one stored connection - only for copying it to another site (M1)."""
        row = self.store.one("SELECT kind, config FROM integrations WHERE id=?", (int(integ_id),))
        if not row:
            return None
        fields = integration_fields(row["kind"])
        sec = {}
        if fields and self.vault:
            for field in fields[1]:
                value = self.vault.get("int:%d:%s" % (int(integ_id), field))
                if value:
                    sec[field] = value
        return row["kind"], json.loads(row["config"] or "{}"), sec

    async def controller_sites(self, integ_id: int) -> dict:
        """The sites a controller connection manages (read only), so a further Lanloom site can read one of them with
        the same account: {"ok", "key", "sites": [{"ref", "name"}]}."""
        parts = self.integration_parts(integ_id)
        if parts is None:
            raise ValueError("integration.err.missing")
        kind, cfg, sec = parts
        if "site" not in (integration_fields(kind) or ((), ()))[0]:
            raise ValueError("site.err.no_site_list")
        if self.demo:
            lister = getattr(self.demo, "controller_sites", None)
            if not callable(lister):
                from .demo_sites import CONTROLLER_SITES
                lister = lambda: [dict(x) for x in CONTROLLER_SITES]      # noqa: E731
            return {"ok": True, "key": "source.controller.ok", "sites": lister(), "current": cfg.get("site") or "default"}
        try:
            src = self._build_source(kind, cfg, sec)
            if hasattr(src, "sites"):
                ok, key, found = await asyncio.wait_for(src.sites(), 40)
            elif hasattr(src, "list_sites"):
                found = await asyncio.wait_for(asyncio.to_thread(src.list_sites), 40)
                ok, key = True, "source.family.ok"
            else:
                raise ValueError("site.err.no_site_list")
        except asyncio.TimeoutError:
            ok, key, found = False, "source.controller.timeout", []
        except ValueError:
            raise
        except Exception as exc:
            ok, key, found = False, getattr(exc, "key", "source.controller.error"), []
        return {"ok": bool(ok), "key": key, "sites": found if ok else [], "current": cfg.get("site") or "default"}

    def delete_integration(self, integ_id: int) -> bool:
        self._stop_source("int:%d" % integ_id)
        ok = self.store.execute("DELETE FROM integrations WHERE id=?", (integ_id,)).rowcount > 0
        if self.vault:
            self.vault.delete_prefix("int:%d:" % integ_id)
        for d in (self.hub, self.mqtt, self.snmp, self.topic_order, self.dns):
            d.pop(integ_id, None)
        self.controlroom.forget_integration(integ_id)
        self.dns_log.forget(integ_id)
        self.topo_obs.pop("int:%d" % integ_id, None)
        self.live_cfg.pop(integ_id, None)
        self._topo_cache = None
        self.source_status.pop("int:%d" % integ_id, None)
        self.structure_version += 1
        return ok

    # ------------------------------------------------------------------ hub bridge (read only)
    def hub_ids(self) -> list:
        return [i["id"] for i in self.integrations() if i["kind"] == "hub"]

    def hub_bridge(self, integ_id: int) -> Optional[dict]:
        if integ_id not in self.hub_ids():
            return None
        return hubbridge.bridge_view(self, integ_id, self.hub.get(integ_id) or hubbridge.new_entry(live.Cadence()))

    def hub_automations(self, integ_id: int) -> Optional[list]:
        if integ_id not in self.hub_ids():
            return None
        return hubbridge.automations_view(self.hub.get(integ_id) or hubbridge.new_entry(live.Cadence()))

    def hub_traces(self, integ_id: int, entity_id: str) -> Optional[dict]:
        return hubbridge.traces_view(self.hub.get(integ_id) or {}, entity_id)

    async def hub_collect_runs(self, wait_s: float = hubbridge.RUNS_WAIT_S) -> None:
        """Before a nightly report: ask each hub connection in admin mode once for the stored run summaries (one
        ``trace/list`` per domain, within the trace budget) and wait for the answer. The outcome per connection lands in
        its entry as ``run_status`` (``ok`` or a key); without admin mode nothing is asked. Never raises."""
        asked = {}
        for integ in self.integrations():
            iid = integ["id"]
            entry = self.hub.get(iid)
            if integ["kind"] != "hub" or not integ.get("enabled") or entry is None:
                continue
            if not (integ["config"].get("admin_mode") or self.demo):
                entry.pop("run_status", None)
                continue
            t0 = time.time()
            if self.demo:
                frame = self.demo.hub_runs() if hasattr(self.demo, "hub_runs") else None
                if frame:
                    self.on_event("int:%d" % iid, iid, frame)
                    entry["run_status"] = {"t": t0, "key": "ok"}
                continue
            src = self._hub_sources.get(iid)
            if src is None or not getattr(src, "want_run_summary", lambda: False)():
                entry["run_status"] = {"t": t0, "key": "offline" if src is None else "unsupported"}
                continue
            entry["run_status"] = {"t": t0, "key": "timeout"}
            asked[iid] = t0
        deadline = time.monotonic() + wait_s
        while asked and time.monotonic() < deadline:
            for iid, t0 in list(asked.items()):
                summary = (self.hub.get(iid) or {}).get("run_summary") or {}
                if summary.get("t", 0) >= t0:
                    asked.pop(iid)
                    self.hub[iid]["run_status"] = {"t": t0, "key": summary.get("error") or "ok"}
            if asked:
                await asyncio.sleep(0.25)

    def hub_refresh_runs(self, integ_id: int, now: Optional[float] = None) -> str:
        """Ask the bridge for a fresh run summary when the views of the effect chains need one: admin mode only, at most
        every ``RUNS_REFRESH_S`` seconds per connection, never one request per automation (the two ``trace/list`` replies
        go out within the bridge's trace budget). Returns ``asked``, ``fresh``, ``admin_off`` or ``offline``."""
        now = time.time() if now is None else now
        integ = next((i for i in self.integrations() if i["id"] == integ_id and i["kind"] == "hub"), None)
        entry = self.hub.get(integ_id)
        if not integ or entry is None or not entry.get("snapshot"):
            return "offline"
        if not (integ["config"].get("admin_mode") or self.demo):
            return "admin_off"
        last = max((entry.get("run_summary") or {}).get("t", 0), entry.get("run_asked", 0))
        if now - last < hubbridge.RUNS_REFRESH_S:
            return "fresh"
        entry["run_asked"] = now
        if self.demo:
            frame = self.demo.hub_runs() if hasattr(self.demo, "hub_runs") else None
            if frame:
                self.on_event("int:%d" % integ_id, integ_id, frame)
                entry["run_status"] = {"t": now, "key": "ok"}
            return "asked"
        src = self._hub_sources.get(integ_id)
        if src is None or not getattr(src, "want_run_summary", lambda: False)():
            return "offline"
        return "asked"

    def hub_chain(self, integ_id: int, entity: Optional[str] = None, device: Optional[str] = None,
                  item: Optional[str] = None) -> Optional[dict]:
        """Effect chains (sensor -> automation -> ... -> device) of the last 24 hours for one entity, one hub device or one
        automation/script, from the stored runs (see hubchain). None when the connection does not exist."""
        if integ_id not in self.hub_ids():
            return None
        now = time.time()
        asked = self.hub_refresh_runs(integ_id, now)
        entry = self.hub.get(integ_id) or {}
        ctx = hubauto.Ctx(entry, now)
        head = {"integration": integ_id, "name": next((i["name"] for i in self.integrations() if i["id"] == integ_id), ""),
                "connected": bool(ctx.snap), "admin": ctx.admin, "t": ctx.snap.get("t"), "now": now, "asked": asked == "asked"}
        return dict(head, **hubauto.chain_view(ctx, entity, device, item))

    def hub_want_traces(self, integ_id: int, entity_id: str) -> dict:
        """Ask the bridge for the last runs of one automation/script (admin mode only, within the trace budget)."""
        integ = next((i for i in self.integrations() if i["id"] == integ_id and i["kind"] == "hub"), None)
        if not integ:
            raise ValueError("hub.err.missing")
        if not integ["config"].get("admin_mode") and not self.demo:
            raise ValueError("hub.err.admin_off")
        if not hub_privacy.valid_entity_id(entity_id) or hub_privacy.domain_of(entity_id) not in ("automation", "script"):
            raise ValueError("hub.err.item")
        if self.demo:
            frame = self.demo.hub_traces(entity_id)
            if frame:
                self.on_event("int:%d" % integ_id, integ_id, frame)
                return {"queued": 1}
            raise ValueError("hub.err.item")
        src = self._hub_sources.get(integ_id)
        if src is None:
            raise ValueError("hub.err.offline")
        place = src.want_traces(entity_id)
        if not place:
            raise ValueError("hub.err.item")
        return {"queued": place}

    # ------------------------------------------------------------------ hub devices and network devices as one component
    def hub_match_rules(self) -> dict:
        rules = self.store.get_setting("hub_match", {}) or {}
        return {"split": [list(p) for p in rules.get("split") or [] if isinstance(p, list) and len(p) == 2],
                "join": [list(p) for p in rules.get("join") or [] if isinstance(p, list) and len(p) == 2]}

    def hub_merge(self, devices: Optional[list] = None) -> dict:
        """Which hub device belongs to which network device (see hubmerge.match); cached until something changes."""
        rules = self.hub_match_rules()
        snaps = {i: (e.get("snapshot") or {}) for i, e in self.hub.items() if e.get("snapshot")}
        key = (self.structure_version, tuple(sorted((i, s.get("t")) for i, s in snaps.items())), json.dumps(rules, sort_keys=True))
        if self._merge_cache and self._merge_cache[0] == key:
            return self._merge_cache[1]
        res = hubmerge.match(devices if devices is not None else self.devices(), snaps, rules)
        self._merge_cache = (key, res)
        return res

    def hub_radio(self, integ_id: int, now: Optional[float] = None) -> dict:
        """Radio networks, freshness per radio device (see hubmerge.radio_view); recomputed at most every 5 s."""
        entry = self.hub.get(integ_id) or {}
        snap = entry.get("snapshot")
        if not snap:
            return {"networks": [], "dev_net": {}, "state": {}}
        now = time.time() if now is None else now
        hit = self._radio_cache.get(integ_id)
        if hit and hit[0] == snap.get("t") and now - hit[1] < 5.0:
            return hit[2]
        res = hubmerge.radio_view(snap, entry.get("states") or {}, now)
        self._radio_cache[integ_id] = (snap.get("t"), now, res)
        return res

    def set_hub_match(self, net_id: str, hub_key: str, action: str) -> dict:
        """split: never draw these two as one; join: they are one (a confirmed suggestion); reset: forget both."""
        if action not in ("split", "join", "reset") or not isinstance(hub_key, str) or not isinstance(net_id, str):
            raise ValueError("hub.match.invalid")
        integ, _, did = hub_key.partition(":")
        if not integ.isdigit() or not did or len(did) > 64 or int(integ) not in self.hub_ids():
            raise ValueError("hub.match.invalid")
        if not self.store.device(net_id):
            raise ValueError("device.err.missing")
        snap = (self.hub.get(int(integ)) or {}).get("snapshot") or {}
        if not any(d.get("id") == did for d in snap.get("devices") or []):
            raise ValueError("hub.match.invalid")
        pair = [net_id, hub_key]
        rules = self.hub_match_rules()
        rules["split"] = [p for p in rules["split"] if p != pair]
        rules["join"] = [p for p in rules["join"] if p[1] != hub_key] if action == "join" else [p for p in rules["join"] if p != pair]
        if action != "reset":
            rules[action].append(pair)
        rules["split"], rules["join"] = rules["split"][-500:], rules["join"][-500:]
        self.store.set_setting("hub_match", rules)
        self._merge_cache = None
        self.structure_version += 1
        return {"ok": True, "action": action}

    def hub_sensors(self, integ_id: int) -> Optional[dict]:
        """The Sensors tab: every entity, areas, devices, "on site" (batteries and radio), radio networks and health."""
        if integ_id not in self.hub_ids():
            return None
        now = time.time()
        entry = self.hub.get(integ_id) or {}
        snap = entry.get("snapshot") or {}
        states = entry.get("states") or {}
        radio = self.hub_radio(integ_id, now)
        merge = self.hub_merge()
        net_of = {k.split(":", 1)[1]: v for k, v in merge["by_hub"].items() if k.startswith("%d:" % integ_id)}
        devs = []
        for d in snap.get("devices") or []:
            if d.get("service"):
                continue
            st = radio["state"].get(d["id"])
            devs.append({"id": d["id"], "name": d.get("name"), "area": d.get("area"), "model": d.get("model"),
                         "manufacturer": d.get("manufacturer"), "sw": d.get("sw"), "radio": d.get("radio"), "role": d.get("role"),
                         "net": radio["dev_net"].get(d["id"]), "fresh": st["fresh"] if st else None,
                         "network_device": net_of.get(d["id"])})
        return {"integration": integ_id, "name": next((i["name"] for i in self.integrations() if i["id"] == integ_id), ""),
                "connected": bool(snap), "t": snap.get("t"), "limited": bool(snap.get("limited")),
                "areas": snap.get("areas") or [], "devices": devs, "entities": hubsensors.entity_rows(snap, states, now),
                "onsite": hubmerge.onsite(snap, states, radio, now), "radio": radio["networks"],
                "health": hubmerge.health(snap, radio),
                "events": [{"platform": p, "per_min": n} for p, n in hubbridge.events_per_integration(entry, now)[:20]]}

    def hub_auto(self, integ_id: int, part: str, entity_id: Optional[str] = None) -> Optional[dict]:
        """The Automations tab (see hubauto): ``tab``, ``item``, ``who``, ``timeline``, ``plan``, ``rooms``, ``board``,
        ``health``. None when the connection does not exist (or the item is unknown); read only."""
        if integ_id not in self.hub_ids():
            return None
        now = time.time()
        entry = self.hub.get(integ_id) or {}
        ctx = hubauto.Ctx(entry, now)
        head = {"integration": integ_id, "name": next((i["name"] for i in self.integrations() if i["id"] == integ_id), ""),
                "connected": bool(ctx.snap), "admin": ctx.admin, "t": ctx.snap.get("t"), "now": now}
        if part == "tab":
            return dict(head, **hubauto.tab(ctx, self.hub_radio(integ_id, now)))
        if part == "item":
            got = hubauto.detail(ctx, entity_id or "")
            return dict(head, **got) if got else None
        if part == "board":
            got = hubauto.board(ctx, entity_id or "")
            return dict(head, **got) if got else None
        if part == "who":
            return dict(head, **hubauto.who(ctx))
        if part == "timeline":
            return dict(head, events=hubauto.timeline(ctx))
        if part == "plan":
            return dict(head, **hubauto.dayplan(ctx))
        if part == "rooms":
            radio = self.hub_radio(integ_id, now)
            merge = self.hub_merge()
            net_of = {k.split(":", 1)[1]: v for k, v in merge["by_hub"].items() if k.startswith("%d:" % integ_id)}
            return dict(head, rooms=hubauto.rooms(ctx, radio["state"], net_of))
        if part == "health":
            return dict(head, **hubauto.health(ctx, self.hub_radio(integ_id, now)))
        return None

    def _hub_autoruns(self) -> list:
        """Automation/script runs since the last live frame (the board of the Automations tab draws a dot for each)."""
        out = []
        for integ_id, entry in self.hub.items():
            pend = entry.get("run_pending")
            if not pend:
                continue
            entry["run_pending"] = {}
            out.extend([integ_id, eid, min(int(n), 20)] for eid, n in pend.items())
        return out[:200]

    async def hub_history(self, integ_id: int, entity_id: str, rng: str) -> dict:
        """One entity's course for the sensor page: 1 h from the live ring, longer ranges from the hub on request."""
        if integ_id not in self.hub_ids():
            raise ValueError("hub.err.missing")
        if rng not in hubsensors.RANGES:
            raise ValueError("hub.err.range")
        entry = self.hub.get(integ_id) or {}
        snap = entry.get("snapshot") or {}
        if not hub_privacy.valid_entity_id(entity_id) or hub_privacy.is_private(entity_id):
            raise ValueError("hub.err.entity")
        ent = next((e for e in snap.get("entities") or [] if e.get("entity_id") == entity_id), None)
        if ent is None:
            raise ValueError("hub.err.entity")
        now = time.time()
        live_state = (entry.get("states") or {}).get(entity_id)
        state = live_state[0] if live_state else ent.get("state")
        devs = {d["id"]: d for d in snap.get("devices") or []}
        areas = {a["id"]: a.get("name") for a in snap.get("areas") or []}
        dev = devs.get(ent.get("device_id")) or {}
        info = {"id": entity_id, "name": ent.get("name") or entity_id, "unit": ent.get("unit"), "domain": ent.get("domain"),
                "dc": ent.get("dc"), "platform": ent.get("platform"), "area": areas.get(ent.get("area") or dev.get("area")),
                "device": dev.get("name"), "device_id": dev.get("id"), "radio": dev.get("radio"),
                "fresh": (self.hub_radio(integ_id, now)["state"].get(dev.get("id")) or {}).get("fresh"),
                "state": state, "changed_s": round(now - live_state[1], 1) if live_state else None}
        recorded = None
        if rng != "1h":
            numeric = hubsensors.is_numeric(ent, state)
            if self.demo is not None and hasattr(self.demo, "hub_history"):
                recorded = self.demo.hub_history(ent, rng, numeric, now)
            else:
                src = self._hub_sources.get(integ_id)
                if src is None:
                    raise ValueError("hub.err.offline")
                from .sources._common import SourceError
                try:
                    recorded = await src.history(entity_id, "24h" if rng == "24h" else rng, numeric, wall=now)
                except SourceError as exc:
                    raise ValueError({"source.hub.busy": "hub.err.busy", "source.hub.offline": "hub.err.offline",
                                      "source.hub.history_item": "hub.err.entity"}.get(exc.key, "hub.err.history")) from None
        return hubsensors.series(info, rng, state, hubsensors.ring_of(entry, entity_id, now), recorded, now)

    def _hub_rings(self) -> list:
        """Event rings of the live frame: state changes of the last second, on the lane of their integration (the
        radio network of the device) or on the trace of their area."""
        out = {}
        for integ_id, entry in self.hub.items():
            pend = entry.get("ring_pending")
            if not pend:
                continue
            entry["ring_pending"] = {}
            idx = entry.get("eidx") or {}
            radio = self.hub_radio(integ_id)
            snap = entry.get("snapshot") or {}
            dev_area = {d["id"]: d.get("area") for d in snap.get("devices") or []}
            for eid, n in pend.items():
                plat, did, area = idx.get(eid) or (None, None, None)
                net = radio["dev_net"].get(did) if did else None
                if net:
                    for tid in ("m:%d:%s" % (integ_id, net), "r:%d:%s" % (integ_id, did)):
                        out[tid] = out.get(tid, 0) + n
                else:
                    area = area or dev_area.get(did)
                    if area:
                        tid = "h:%d:%s" % (integ_id, area)
                        out[tid] = out.get(tid, 0) + n
        return sorted(([t, min(n, 20)] for t, n in out.items()), key=lambda x: -x[1])[:200]

    def reload_after_restore(self) -> None:
        """A backup was restored into the database: drop what was read from the old configuration and start the
        restored connections (lanloom/backup.py)."""
        for key in [k for k in self._source_tasks if k.startswith("int:")]:
            self._stop_source(key)
        for d in (self.hub, self.mqtt, self.snmp, self.topic_order, self.live_cfg):
            d.clear()
        for key in [k for k in self.topo_obs if k.startswith("int:")]:
            self.topo_obs.pop(key, None)
        for key in [k for k in self.source_status if k.startswith("int:")]:
            self.source_status.pop(key, None)
        self._topo_cache = None
        self._ident_cache = None
        self._manual_ver += 1
        self.structure_version += 1
        self._refresh_index()
        self.agents.reload()
        if not self.demo and not self.cfg.probe and self._tasks:
            for integ in self.integrations():
                if integ["enabled"]:
                    self._start_integration(integ)

    def _start_integration(self, integ: dict) -> None:
        sec = {}
        if self.vault:
            for field in (integration_fields(integ["kind"]) or ((), ()))[1]:
                value = self.vault.get("int:%d:%s" % (integ["id"], field))
                if value:
                    sec[field] = value
        self._start_source("int:%d" % integ["id"], integ["kind"], integ["config"], sec, integ["id"])

    def _start_source(self, key: str, kind: str, cfg: dict, sec: Optional[dict] = None, integ_id: Optional[int] = None) -> None:
        async def runner():
            try:
                if kind == "host":
                    from .sources import HostNet
                    src = HostNet()
                else:
                    src = self._build_source(kind, cfg, sec or {})
            except Exception as exc:
                module = kind if kind in INTEGRATION_FIELDS or kind == "host" else "family"
                self.source_status[key] = {"ok": False, "key": getattr(exc, "key", "source.%s.config" % module), "t": time.time()}
                return
            if kind not in INTEGRATION_FIELDS and kind != "host" and await self._scope_refused(src):
                self.source_status[key] = {"ok": False, "key": "integration.err.can_write", "t": time.time()}
                return
            if kind == "hub":
                self._hub_sources[integ_id] = src       # for traces on request (admin mode)
            try:
                await src.run(lambda ev: self.on_event(key, integ_id, ev))
            finally:
                if self._hub_sources.get(integ_id) is src:
                    self._hub_sources.pop(integ_id, None)

        self._stop_source(key)
        self._source_tasks[key] = asyncio.create_task(runner())

    def _stop_source(self, key: str) -> None:
        task = self._source_tasks.pop(key, None)
        if task:
            task.cancel()

    # ------------------------------------------------------------------ events from sources
    def on_event(self, key: str, integ_id: Optional[int], ev: dict) -> None:
        etype = ev.get("type")
        now = time.time()
        if etype == "status":
            self.source_status[key] = {"ok": bool(ev.get("ok")), "key": ev.get("key"), "t": ev.get("t") or now}
            return
        cad = self.cadence.setdefault(key, live.Cadence())
        if etype == "bytes":
            cad.tick(now)
            self._book_flows(ev.get("flows") or [], now)
        elif etype == "iface":
            for itf in ev.get("ifaces") or []:
                self.self_iface[itf.get("name")] = [itf.get("tx") or 0, itf.get("rx") or 0]
                if key == "host":
                    self.counters.feed("host_if", (itf.get("tx") or 0) + (itf.get("rx") or 0), now)
        elif etype in ("hub_snapshot", "hub_state", "hub_triggers", "hub_deps", "hub_traces", "hub_runs", "hub_bridge"):
            cad.tick(now)
            entry = self.hub.get(integ_id)
            if entry is None:
                entry = self.hub[integ_id] = hubbridge.new_entry(live.Cadence())
            hubbridge.book(entry, ev, now)
            if etype == "hub_snapshot":
                self.structure_version += 1
            elif etype == "hub_state":
                entry["events"].tick(now)
        elif etype == "mqtt":
            cad.tick(now)
            self._book_mqtt(integ_id, ev, now)
        elif etype == "snmp":
            cad.tick(now)
            self._book_snmp(integ_id, ev, now)
            self.controlroom.feed(integ_id, etype, ev, now)
        elif etype == "nut":
            cad.tick(now)
            self.controlroom.feed(integ_id, etype, ev, now)
        elif etype == "config":
            cad.tick(now)
            self._book_config(integ_id, ev, now)
        elif etype == "dns":
            cad.tick(now)
            self._book_dns(integ_id, ev, now)
        elif etype == "topology":
            cad.tick(now)
            self.topo_obs[key] = {"integ": integ_id, "ev": ev}
            self._topo_cache = None
            try:
                if ev.get("ports"):
                    self._ports_light[integ_id] = now
                    self.book_ports(ev.get("ports"), ev.get("equipment"), now)
                self.book_roaming(ev, now)
            except Exception:
                log.exception("booking ports failed")
            try:
                self.book_adapter_devices(now)
            except Exception:
                log.exception("booking adapter devices failed")
            sig = self.topology()["signature"]
            if sig != self._topo_sig:
                self._topo_sig = sig
                self.structure_version += 1

    def dns_kinds(self) -> list:
        """Ids of the built-in families that read a DNS filter (one per supported product)."""
        from . import integrations
        return integrations.families_reading("dns")

    def _book_dns(self, integ_id, ev: dict, now: float) -> None:
        """One reading of a DNS filter: kept for the views, its hour and day sums written (totals only)."""
        try:
            st = dnsfilter.clean(ev.get("state"))
        except (dnsfilter.ModelError, TypeError, ValueError):
            self.source_status["int:%d" % integ_id] = {"ok": False, "key": "source.family.protocol", "t": now}
            return
        first = integ_id not in self.dns
        self.dns[integ_id] = {"state": st, "t": now}
        try:
            self.dns_log.record(integ_id, st, now)
        except Exception:
            log.exception("writing DNS filter sums failed")
        if first:
            self.structure_version += 1

    def _book_config(self, integ_id, ev: dict, now: float) -> None:
        """Neutral configuration from a family read half (shown read-only in the firewall tab)."""
        from .firewall import neutral
        from .integrations import capabilities as capmod
        st = ev.get("state")
        try:
            neutral.validate(st)
            caps = capmod.validate(ev.get("capabilities"), ev.get("family"))
        except (neutral.ModelError, capmod.CapabilityError):
            self.source_status["int:%d" % integ_id] = {"ok": False, "key": "source.family.protocol", "t": now}
            return
        caps["write"] = {}                 # a read half never offers writes, whatever it says
        if st.get("hosts") is not None:
            st = dict(st, findings=list(st.get("findings") or []) + self._vlan_chain(integ_id, ev.get("family"), st))
        self.live_cfg[integ_id] = {"state": st, "caps": caps, "family": ev.get("family"), "t": now}
        try:
            integs = self.integrations()
            self.fwdrift.feed("int:%d" % integ_id, next((i["name"] for i in integs if i["id"] == integ_id), ""), ev.get("family"), st, now,
                              keep={"int:%d" % i["id"] for i in integs})
        except Exception:
            log.exception("comparing the firewall configuration failed")
        try:
            if st.get("wan_events"):
                self.controlroom.wan.ingest_gateway_events(st["wan_events"])
        except Exception:
            log.exception("booking gateway events failed")
        try:
            self.hitlog.feed("int:%d" % integ_id, st.get("policies"), now)
            if st.get("ports") and now - self._ports_light.get(integ_id, 0) > 180:       # no light poll carries ports: use the full one
                self.book_ports(st["ports"], [{"id": e["id"], "ip": e.get("mgmt_ip")} for e in st.get("equipment") or []], now)
        except Exception:
            log.exception("booking firewall hits failed")

    # ------------------------------------------------------------------ switch ports and roaming (S4)
    def book_ports(self, ports, equipment, now: float) -> None:
        """Ports of a controller: link drops and errors into the watch; where a cable device sits alone on a port, the
        port counters give its exact bytes on the physical link (instead of the host's own flows)."""
        deltas = self.portwatch.feed(ports, now)
        try:
            portnotify.check(self, now)
        except Exception:
            log.exception("port notification failed")
        if not deltas or not equipment:
            return
        port_of = {p.get("id"): p for p in ports or [] if isinstance(p, dict)}
        node_of = {}
        topo = self.topology()
        for e in equipment:
            node = self.ip_to_device.get(e.get("ip") or "")
            if not node and e.get("mac"):
                node = next((vid for vid, v in topo["nodes"].items() if v.get("mac") == e["mac"]), None)
            if node:
                node_of[e["id"]] = node
        kids = {}
        for child, link in topo["links"].items():
            if link["medium"] not in ("radio", "virtual") and link.get("port"):
                kids.setdefault((link["parent"], link["port"]), []).append(child)
        exact = []
        for pid, d_tx, d_rx in deltas:
            p = port_of.get(pid) or {}
            node = node_of.get(p.get("device"))
            below = kids.get((node, str(p.get("idx")))) if node else None
            if below and len(below) == 1:
                exact.append((node, below[0], d_tx, d_rx))
        if self.store.get_setting("port_noise", False):
            exact = self._without_noise(exact)
        for node, child, d_tx, d_rx in exact:
            trace = "p:%s:%s" % (node, child)
            old = self.spread.get(trace)
            carry0, carry1 = (old[0], old[1]) if old else (0, 0)
            self.spread[trace] = [carry0 + d_tx, carry1 + d_rx, 60]    # a = switch, b = the cable device: tx leaves the switch
            self.port_exact[trace] = now

    NOISE_MIN_PORTS = 3            # a network needs this many measured ports before a common floor means anything

    def _without_noise(self, exact: list) -> list:
        """Takes the background noise out of the exact port counters: what every device of one network receives anyway
        (broadcasts and multicast flooded to all ports) is the smallest sent rate among at least three of its ports;
        it is subtracted from each of them. Switched off by default (setting ``port_noise``)."""
        by_ip = {d["id"]: d["ip"] for d in self.devices()}
        nets = []
        for entry in self.live_configs():
            for n in entry["state"].get("networks") or []:
                try:
                    nets.append((ipaddress.ip_network(n.get("cidr") or "", strict=False), n["id"]))
                except (ValueError, KeyError):
                    continue
        groups = {}
        for i, (_, child, d_tx, _) in enumerate(exact):
            try:
                addr = ipaddress.ip_address(by_ip.get(child) or "")
            except ValueError:
                continue
            best = max((n for n in nets if addr in n[0]), key=lambda n: n[0].prefixlen, default=None)
            if best:
                groups.setdefault(best[1], []).append(i)
        out = list(exact)
        for idx in groups.values():
            if len(idx) < self.NOISE_MIN_PORTS:
                continue
            floor = min(exact[i][2] for i in idx)
            for i in idx:
                node, child, d_tx, d_rx = exact[i]
                out[i] = (node, child, max(0, d_tx - floor), d_rx)
        return out

    def net_tags(self) -> dict:
        """{device id: {"vlan", "net", "ssid", "band"}}: the network (name, VLAN) a device sits in and, for a wireless
        device, the wireless network name and band - the short tags of the device list and its filter."""
        nets = []
        for entry in self.live_configs():
            for n in entry["state"].get("networks") or []:
                try:
                    nets.append((ipaddress.ip_network(n.get("cidr") or "", strict=False), n.get("name"), n.get("vlan")))
                except ValueError:
                    continue
        try:
            links = self.topology()["links"] if self.topo_obs else {}
        except Exception:
            links = {}
        out = {}
        for d in self.devices():
            tag = {"vlan": None, "net": None, "ssid": None, "band": None}
            try:
                addr = ipaddress.ip_address(d["ip"] or "")
            except ValueError:
                addr = None
            if addr is not None:
                best = max((n for n in nets if addr in n[0]), key=lambda n: n[0].prefixlen, default=None)
                if best:
                    tag["net"], tag["vlan"] = best[1], best[2] if isinstance(best[2], int) and not isinstance(best[2], bool) else None
            link = links.get(d["id"]) or {}
            if link.get("medium") == "radio":
                tag["ssid"], tag["band"] = link.get("ssid"), link.get("band")
            if any(tag.values()):
                out[d["id"]] = tag
        return out

    def book_roaming(self, ev: dict, now: float) -> None:
        wireless = [{"id": c.get("mac"), "ap": c.get("parent_mac"), "band": c.get("band"), "signal": c.get("signal"),
                     "name": c.get("name") or c.get("ip")} for c in ev.get("clients") or []
                if c.get("medium") == "radio" and c.get("mac") and c.get("parent_mac")]
        if wireless or any(c.get("medium") == "radio" for c in ev.get("clients") or []):
            self.roamwatch.feed(wireless, now)

    def ap_names(self) -> dict:
        """{access point key (hardware address): name} of the access points this site's controller adapters read and
        that are online - the places a wireless device could go instead of the one it clings to."""
        mac_of = {e.get("id"): e.get("mac") for o in self.topo_obs.values() for e in (o.get("ev") or {}).get("equipment") or []}
        out = {}
        for entry in self.live_configs():
            for e in entry["state"].get("equipment") or []:
                if (e.get("kind") == "ap" or e.get("radios")) and e.get("online") is not False and mac_of.get(e.get("id")):
                    out[mac_of[e["id"]]] = e.get("name") or "?"
        return out

    def sticky_clients(self, now: float = None) -> list:
        """Wireless devices that sit at -80 dBm or weaker on one access point for ten minutes although the site has another
        (memory only, see ``portfront.RoamWatch.stuck``)."""
        return self.roamwatch.stuck(self.ap_names(), time.time() if now is None else now)

    def port_fronts(self, now: float = None) -> list:
        """Faceplates of every switch and access point the controller adapters read (see ``portfront``)."""
        now = time.time() if now is None else now
        out = []
        ap_total = len(self.ap_names()) if self.live_cfg else 0
        topo = self.topology() if self.live_cfg else {"links": {}, "nodes": {}}
        dev_by_id = {d["id"]: d for d in self.devices()} if self.live_cfg else {}
        for entry in self.live_configs():
            st = entry["state"]
            if not st.get("ports") and not st.get("equipment"):
                continue
            ap_keys, behind = {}, {}
            mac_of = {e.get("id"): e.get("mac") for o in self.topo_obs.values() for e in (o.get("ev") or {}).get("equipment") or []}
            for e in st.get("equipment") or []:
                if mac_of.get(e["id"]):
                    ap_keys[e["id"]] = mac_of[e["id"]]
                node = self.ip_to_device.get(e.get("mgmt_ip") or "")
                if node:
                    for child, link in topo["links"].items():
                        if link["parent"] == node and link["medium"] not in ("radio", "virtual") and str(link.get("port") or "").isdigit():
                            dev = dev_by_id.get(child) or topo["nodes"].get(child) or {}
                            behind.setdefault(e["id"], {}).setdefault(int(link["port"]), []).append(
                                {"name": dev.get("title") or dev.get("name") or dev.get("ip") or "?", "kind": dev.get("kind") or "other",
                                 "wired": True, "ip": dev.get("ip")})
            for f in portfront.fronts(st, self.portwatch, self.roamwatch, now, behind=behind, ap_keys=ap_keys, ap_total=ap_total):
                out.append(dict(f, source=entry["key"]))
        return out

    def _vlan_chain(self, integ_id, family, st: dict) -> list:
        """Cross-source finding of a hypervisor: a guest VLAN the switch port of its node does not carry (topology
        tells the port, a controller adapter its VLAN settings). Empty when either side is unknown."""
        from . import integrations
        try:
            hvmodel = integrations.load_model(family)
        except (KeyError, ImportError):
            return []
        if not hasattr(hvmodel, "vlan_chain"):
            return []
        switches = [e["state"] for i, e in self.live_cfg.items() if i != integ_id and (e.get("state") or {}).get("ports")]
        if not switches:
            return []
        try:
            topo = self.topology()
        except Exception:
            return []
        ip_of = {dev: ip for ip, dev in self.ip_to_device.items()}
        uplinks = {}
        for h in st.get("hosts") or []:
            link = topo["links"].get(self.ip_to_device.get(h.get("ip") or ""))
            if link and link.get("port") and link.get("medium") != "virtual":
                parent = link["parent"]
                uplinks[h["id"]] = {"ip": ip_of.get(parent) or (topo["nodes"].get(parent) or {}).get("ip"), "port": link["port"]}
        try:
            return hvmodel.vlan_chain(st, uplinks, switches)
        except Exception:
            return []

    # ------------------------------------------------------------------ devices only an adapter knows
    def book_adapter_devices(self, now: Optional[float] = None, force: bool = False) -> int:
        """Book the devices the adapters report (clients in other VLANs, switch address tables, LLDP neighbours) into
        the inventory. No packet is sent. At most every 30 s; switched off by the setting ``adapter_inventory``."""
        now = time.time() if now is None else now
        if not self.store.get_setting("adapter_inventory", True):
            return 0
        if not force and 0 <= now - self._adapter_t < 30:
            return 0
        self._adapter_t = now
        own = {d["mac"] for d in self.store.devices() if d["data"].get("is_self") and d.get("mac")}
        if not self.demo:
            from .scan import netinfo
            own.update(netinfo.iface_macs().values())       # this host's own cards, also before a scan wrote them
        self._drop_own_adapter_rows(own)
        return self.ingest_adapter(adapter_inventory.records(self.topo_obs, own), now)

    def _drop_own_adapter_rows(self, own: set) -> int:
        """A device only an adapter booked under a hardware address that is one of this host's own interfaces is this
        host seen from the switch (booked by an earlier version, before the host's address was known): remove it,
        the next scan writes the address on "This host"."""
        gone = []
        for d in self.store.devices():
            if (d.get("mac") or "").lower() in own and adapter_inventory.is_adapter_only(d["data"]):
                gone.append(d["id"])
        for did in gone:
            self.store.execute("DELETE FROM devices WHERE id=?", (did,))
            self.store.execute("DELETE FROM phys_manual WHERE device_id=? OR parent_id=?", (did, did))
            self.inventory.forget(did)
            self.paths.forget_device(did)
            self.availability.forget("dev:" + did)
            self.vulns.forget_device(did)
            self.presence.pop(did, None)
        if gone:
            self._manual_ver += 1
            self._refresh_index()
        return len(gone)

    def ingest_adapter(self, recs: list, now: Optional[float] = None) -> int:
        """Put adapter records into the inventory. A device a scan already knows only gets the adapter's facts added
        (and an address it did not have); a new device is classified like a scanned one. Returns the number of new
        devices. The first booking after the feature started does not announce its devices as "new" in the night
        report - they were there before."""
        now = time.time() if now is None else now
        rows = {r["id"]: r for r in self.store.query("SELECT * FROM devices")}
        adapter_count = sum(1 for r in rows.values() if '"sources":["adapter"]' in r["data"])
        fresh, touched = [], []
        with self.store.transaction():
            for rec in recs:
                host = adapter_inventory.host_of(rec)
                did = classify.device_id(host)
                row = rows.get(did)
                if row is None:
                    if adapter_count >= adapter_inventory.MAX_RECORDS:
                        continue
                    adapter_count += 1
                    fresh.append(host)
                    continue
                data = json.loads(row["data"])
                changed = data.get("adapter") != host["adapter"]
                data["adapter"] = host["adapter"]
                ip = row["ip"]
                if not ip and rec.get("ip"):
                    ip, changed = rec["ip"], True
                    data["ip"] = ip
                if rec.get("name") and rec["name"] not in (data.get("hostnames") or []) and len(data.get("hostnames") or []) < 8:
                    data["hostnames"] = (data.get("hostnames") or []) + [rec["name"]]
                    changed = True
                if changed:
                    self.store.execute("UPDATE devices SET ip=?, data=? WHERE id=?",
                                       (ip, json.dumps(data, separators=(",", ":")), did))
                touched.append(did)
        new_ids = []
        if fresh:
            self.ingest({"hosts": fresh}, None)
            new_ids = [classify.device_id(h) for h in fresh]
            for h in fresh:      # infrastructure the controller names by role is not an "unknown" device
                role = (h.get("adapter") or {}).get("role")
                if role in adapter_inventory.ROLE_KINDS:
                    self.store.execute("UPDATE devices SET kind=?, confidence=? WHERE id=? AND kind='unknown'",
                                       (adapter_inventory.ROLE_KINDS[role], 0.9, classify.device_id(h)))
        for rec in recs:
            if not rec.get("offline"):         # a device the controller lists as disconnected is no sign of life
                self.presence[classify.device_id(adapter_inventory.host_of(rec))] = now
        if new_ids or touched:
            self._refresh_index()
        seeded = self.store.get_setting("adapter_seeded", False)
        if recs and not seeded:
            self.store.set_setting("adapter_seeded", True)
            journal = getattr(getattr(self, "reports", None), "watch", None)
            if journal is not None and new_ids and getattr(journal, "seeded", False):
                journal.known.update(new_ids)
                journal.save()
        return len(new_ids)

    # ------------------------------------------------------------------ zones (networks and VLANs)
    def zone_consent(self) -> list:
        return [c for c in (self.store.get_setting("zone_consent", []) or []) if isinstance(c, str)][:7]

    def zone_list(self) -> dict:
        """Every network Lanloom knows (own interfaces, adapter networks and VLANs, routes) with how far it may look
        into each, and the devices of each. No packet is sent."""
        routes = []
        if not self.demo and not self.remote:          # this host's routing table belongs to the home site
            from .scan import netinfo
            try:
                with open(netinfo.ROUTE_PATH, encoding="ascii", errors="replace") as fh:
                    routes = netinfo.parse_routes(fh.read(1 << 20))
            except OSError:
                pass
        states = [{"key": e["key"], "name": e["name"], "state": e["state"]} for e in self.live_configs()]
        zs = zones_mod.build(self.networks(), states, routes, self.zone_consent())
        zs, unzoned = zones_mod.assign(zs, self.devices())
        macless = sum(1 for d in self.store.devices() if not d["ip"])
        return {"zones": zs, "unzoned": len(unzoned), "no_address": macless,
                "adapter_inventory": bool(self.store.get_setting("adapter_inventory", True))}

    def set_zone_consent(self, cidr: str, on: bool) -> dict:
        """The user agrees (or withdraws) that a scan may cross a router into one range. Only ranges the zone list
        knows, only ones the scanner would accept at all."""
        from .scan import safety
        net = safety.parse_cidr(cidr)
        ok, key = safety.check_cidr(cidr)
        if net is None or not ok:
            raise ValueError(key if net is not None else "scan.unsafe.invalid")
        known = {z["cidr"] for z in self.zone_list()["zones"]}
        if str(net) not in known:
            raise ValueError("zone.err.unknown")
        cur = [c for c in self.zone_consent() if c != str(net)]
        if on:
            if len(cur) >= safety.MAX_RANGES - 1:
                raise ValueError("scan.unsafe.too_many")
            cur.append(str(net))
        self.store.set_setting("zone_consent", cur)
        return self.zone_list()

    def live_configs(self, max_age_s: float = 1200.0) -> list:
        """Fresh configurations of family read halves, newest integration first in id order."""
        now = time.time()
        out = []
        names = {i["id"]: i["name"] for i in self.integrations()} if self.live_cfg else {}
        for integ_id, entry in sorted(self.live_cfg.items()):
            if now - entry["t"] <= max_age_s and integ_id in names:
                out.append(dict(entry, key="int:%d" % integ_id, name=names[integ_id], age_s=int(now - entry["t"])))
        return out

    def _add_bytes(self, trace: str, b0: int, b1: int) -> None:
        acc = self.pending_bytes.setdefault(trace, [0, 0])
        acc[0] += max(0, int(b0 or 0))
        acc[1] += max(0, int(b1 or 0))

    def _own_id(self) -> Optional[str]:
        """Id of this host's own entry: the entry marked as this host, else the one with its address."""
        return self.self_device or (self.ip_to_device.get(self.self_ip) if self.self_ip else None)

    def _book_flows(self, flows: list, now: float) -> None:
        self_id = self._own_id() or "self"
        router_id = self.ip_to_device.get(self.gateway_ip) if self.gateway_ip else None
        for fl in flows:
            tx, rx = fl.get("tx"), fl.get("rx")
            if tx is None and rx is None:
                continue
            peer = fl.get("peer_ip") or ""
            dev_id = self.ip_to_device.get(peer)
            if dev_id and dev_id != self_id or (not dev_id and _is_outside(peer)):
                self.counters.feed("host_flow", (tx or 0) + (rx or 0), now)   # bytes that cross a counted interface
            if dev_id and dev_id != self_id:
                self._add_bytes(flow_trace(self_id, dev_id), tx, rx)
                self._book_path(self_id, dev_id, tx, rx)
                self.flow_seen[dev_id] = now
                self._book_port(dev_id, fl.get("peer_port"), now)
            else:
                if router_id:
                    self._add_bytes(lan_trace(router_id, self_id), rx, tx)     # router -> self is "a -> b"
                    self._book_path(self_id, router_id, tx, rx)
                if not dev_id and _is_outside(peer):
                    self._book_outside(peer, tx, rx, now)
                    if not self.demo:          # the demo books synthetic data paths (its peers are documentation addresses)
                        self.paths.book(self_id, peer, tx, rx, now)

    def phys_path(self, a: str, b: str) -> Optional[list]:
        """Physical links between two components: [(trace id, +1 = with the trace a->b | -1 = against it)], or None."""
        links = self.topology()["links"]

        def chain(node):
            out, seen = [node], {node}
            while node in links and len(out) < 64:
                node = links[node]["parent"]
                if node in seen:
                    return out
                seen.add(node)
                out.append(node)
            return out

        up, down = chain(a), chain(b)
        common = next((n for n in up if n in set(down)), None)
        if common is None or a == b:
            return None
        path = [("p:%s:%s" % (links[n]["parent"], n), -1) for n in up[:up.index(common)]]
        path += [("p:%s:%s" % (links[n]["parent"], n), +1) for n in reversed(down[:down.index(common)])]
        return path

    def _book_path(self, a: str, b: str, sent, received) -> None:
        """Bytes between two components also travel along the physical links in between (network view dots)."""
        path = self.phys_path(a, b)
        now = time.time()
        for trace, sense in path or ():
            if now - self.port_exact.get(trace, 0) < 180:
                continue                       # the port counters of a switch already count every byte on this link
            if sense > 0:
                self._add_bytes(trace, sent, received)                     # a -> b of the trace is parent -> child
            else:
                self._add_bytes(trace, received, sent)

    @staticmethod
    def _decay(value: float, since: float, now: float) -> float:
        return value * math.exp(-max(0.0, now - since) / PEER_TAU)

    def _book_outside(self, peer: str, tx, rx, now: float) -> None:
        e = self.outside_peers.get(peer)
        if e is None:
            if len(self.outside_peers) >= 512:          # bounded: forget the lightest peer
                del self.outside_peers[min(self.outside_peers, key=lambda k: self.outside_peers[k]["w"])]
            e = self.outside_peers[peer] = {"out": 0.0, "in": 0.0, "w": 0.0, "t": now}
        k = math.exp(-max(0.0, now - e["t"]) / PEER_TAU)
        out, inn = max(0, int(tx or 0)), max(0, int(rx or 0))
        e["out"] = e["out"] * k + out
        e["in"] = e["in"] * k + inn
        e["w"] = e["w"] * k + out + inn
        e["t"] = now

    def _book_port(self, dev_id: str, port, now: float) -> None:
        if not isinstance(port, int) or not 0 < port < 65536:
            return
        ports = self.flow_ports.setdefault(dev_id, {})
        c = ports.get(port)
        ports[port] = [(self._decay(c[0], c[1], now) if c else 0.0) + 1.0, now]

    def outside_list(self, now: float) -> list:
        """Outside peers of the last PEER_KEEP seconds, heaviest first (for the end marks at the internet tile)."""
        for peer in [p for p, e in self.outside_peers.items() if now - e["t"] > PEER_KEEP]:
            del self.outside_peers[peer]
        rows = []
        for peer, e in self.outside_peers.items():
            k = math.exp(-max(0.0, now - e["t"]) / PEER_TAU)
            rows.append({"id": peer, "out": round(e["out"] * k), "in": round(e["in"] * k), "weight": round(e["w"] * k)})
        rows.sort(key=lambda r: (-r["weight"], r["id"]))
        return rows[:PEER_MAX]

    def seen_ports(self, now: float) -> dict:
        """Target ports seen in host flows per device (most frequent first, at most two), last PEER_KEEP seconds."""
        out = {}
        for dev_id, ports in list(self.flow_ports.items()):
            for p in [p for p, c in ports.items() if now - c[1] > PEER_KEEP]:
                del ports[p]
            if not ports:
                del self.flow_ports[dev_id]
                continue
            ranked = sorted(ports.items(), key=lambda pc: (-self._decay(pc[1][0], pc[1][1], now), pc[0]))
            out[dev_id] = [p for p, _ in ranked[:2]]
        return out

    def _book_mqtt(self, integ_id, ev: dict, now: float) -> None:
        entry = self.mqtt.setdefault(integ_id, {"topics": {}, "t": now, "broker": {}})
        entry["t"] = now
        if ev.get("broker"):
            entry["broker"] = ev["broker"]
        dt = float(ev.get("dt") or 1.0) or 1.0
        for root, msgs, nbytes in ev.get("topics") or []:
            topic = entry["topics"].setdefault(root, {"msgs": 0.0, "bytes": 0.0, "last": 0.0, "total": 0, "cad": live.Cadence()})
            a = 0.2
            topic["msgs"] += a * (msgs / dt - topic["msgs"])
            topic["bytes"] += a * (nbytes / dt - topic["bytes"])
            topic["total"] += nbytes
            if msgs:
                topic["last"] = now
                topic["cad"].tick(now)
            if root in self.topic_order.get(integ_id, []):
                self._add_bytes(topic_trace(integ_id, root), nbytes, 0)
        # decay topics without traffic in this frame
        seen = {r for r, _, _ in ev.get("topics") or []}
        for root, topic in entry["topics"].items():
            if root not in seen:
                topic["msgs"] *= 0.8
                topic["bytes"] *= 0.8
        order = self.topic_order.setdefault(integ_id, [])
        if len(order) < 8:
            ranked = sorted(entry["topics"].items(), key=lambda kv: kv[1]["total"], reverse=True)
            for root, _ in ranked:
                if root not in order and len(order) < 8 and not root.startswith("$"):
                    order.append(root)
                    self.structure_version += 1

    def _book_snmp(self, integ_id, ev: dict, now: float) -> None:
        prev = self.snmp.get(integ_id) or {}
        if "hr" in ev:
            ev["hr_t"] = now                   # host values come once a minute; the frames between keep the last reading
        elif prev.get("hr") is not None:
            ev["hr"], ev["hr_t"] = prev["hr"], prev.get("hr_t")
        self.snmp[integ_id] = ev
        integ = self.store.one("SELECT config FROM integrations WHERE id=?", (integ_id,)) if integ_id else None
        cfg = json.loads(integ["config"]) if integ else {}
        host_dev = self.ip_to_device.get(cfg.get("host") or ev.get("host") or "")
        if not host_dev:
            return
        if host_dev == self.ip_to_device.get(self.gateway_ip or ""):
            itf = pick_uplink(ev.get("ifaces") or [], cfg.get("uplink") or self.store.get_setting("uplink_iface", ""))
            if itf and itf.get("in") is not None:
                self.counters.feed("wan", (itf.get("in") or 0) + (itf.get("out") or 0), now)
                # a poll covers ~10 s: spread the delta over the coming seconds instead of one burst (a = internet, b = router)
                left = self.spread.get(uplink_trace(host_dev))
                carry0, carry1 = (left[0], left[1]) if left else (0, 0)
                self.spread[uplink_trace(host_dev)] = [carry0 + (itf.get("in") or 0), carry1 + (itf.get("out") or 0), 10]

    # ------------------------------------------------------------------ live loop
    def _source_grade(self, key: str) -> str:
        cad = self.cadence.get(key)
        st = self.source_status.get(key)
        if st and not st.get("ok") and (not cad or not cad.last or time.time() - cad.last > 30):
            return "interrupted"
        return cad.grade() if cad else "unknown"

    def source_list(self) -> list:
        out = []
        names = {"host": "source.name.host", "scan": "source.name.scan", "presence": "source.name.presence"}
        for key, cad in sorted(self.cadence.items()):
            item = {"id": key, "name": names.get(key, key), "grade": self._source_grade(key),
                    "cadence_s": round(cad.cadence(), 1) if cad.cadence() else None,
                    "age_s": round(time.time() - cad.last, 1) if cad.last else None}
            if key.startswith("int:"):
                row = self.store.one("SELECT kind, name FROM integrations WHERE id=?", (int(key[4:]),))
                if not row:
                    continue
                item["name"] = row["name"]
                item["kind"] = row["kind"]
            out.append(item)
        return out

    async def _tick_loop(self) -> None:
        last = time.time()
        while True:
            await asyncio.sleep(max(0.05, 1.0 - (time.time() - last)))
            now = time.time()
            dt, last = now - last, now
            try:
                frame = self.tick(now, dt)
            except Exception:
                log.exception("live tick failed")
                continue
            if self.clients:
                text = json.dumps(frame, separators=(",", ":"))
                for q in list(self.clients):
                    if q.qsize() < 5:
                        q.put_nowait(text)

    def tick(self, now: float, dt: float = 1.0) -> dict:
        self.quant.check_q(now)
        try:
            self.counters.tick(now)
        except Exception:
            log.exception("counter check failed")
        if int(now) % 60 == 0:
            self.ring.prune(now)
        try:
            self.paths.maybe_flush(now)
            self.availability.maybe_flush(now)
            self.traffic_log.maybe_flush(now)
        except Exception:
            log.exception("data paths flush failed")
        for trace in list(self.spread):
            b0, b1, left = self.spread[trace]
            share0, share1 = (b0, b1) if left <= 1 else (b0 // left, b1 // left)
            self._add_bytes(trace, share0, share1)
            if left <= 1:
                del self.spread[trace]
            else:
                self.spread[trace] = [b0 - share0, b1 - share1, left - 1]
        pending, self.pending_bytes = self.pending_bytes, {}
        dots, rates = [], {}
        for trace, (b0, b1) in pending.items():
            n0, n1 = self.quant.book(trace, b0, b1, now, dt)
            if n0:
                dots.append([trace, 0, n0])
            if n1:
                dots.append([trace, 1, n1])
            self.ring.push(trace, now, b0 / max(dt, 0.2), b1 / max(dt, 0.2))
            self.traffic_log.book(trace, b0, b1, now)
        for (trace, direction), r in self.quant.rates(now).items():
            rates.setdefault(trace, [0, 0])[direction] = round(r)
        from . import mapmodel
        grades, metrics = mapmodel.live_overlay(self, now, rates)
        sources = self.source_list()
        # every frame names its site (M1T): the page drops a frame of another site instead of drawing it
        frame = {"site": self.site_id, "t": round(now, 3), "q": self.quant.q, "dots": dots, "rates": rates, "grades": grades, "metrics": metrics,
                 "overall": {"grade": live.typical([s["grade"] for s in sources]), "sources": sources},
                 "structure": self.structure_version, "mapver": self.map_change(now), "pathsver": self.paths.version}
        # what the end and port marks of the board show: peers beyond the LAN (at the internet tile) and target ports of host flows
        router_id = self.ip_to_device.get(self.gateway_ip) if self.gateway_ip else None
        peers = self.outside_list(now)
        if peers and router_id:
            frame["peers"] = {"internet": peers}
        self_id = self._own_id()
        ports = self.seen_ports(now)
        if ports:
            frame["ports"] = {flow_trace(self_id or "self", dev_id): p for dev_id, p in ports.items()}
        rings = self._hub_rings()
        if rings:
            frame["rings"] = rings
        autoruns = self._hub_autoruns()
        if autoruns:
            frame["autoruns"] = autoruns
        self.last_frame = frame
        self.overview.maybe_sample(now)
        return frame


# ---------------------------------------------------------------------- trace ids (shared with mapmodel)
def flow_trace(self_id: str, dev_id: str) -> str:
    return "f:%s:%s" % (self_id, dev_id)


def lan_trace(router_id: str, dev_id: str) -> str:
    return "l:%s:%s" % (router_id, dev_id)


def uplink_trace(router_id: str) -> str:
    return "u:internet:%s" % router_id


def topic_trace(integ_id, root: str) -> str:
    return "q:%s:%s" % (integ_id, root)


def pick_uplink(ifaces: list, preferred: str = "") -> Optional[dict]:
    if preferred:
        for itf in ifaces:
            if itf.get("name") == preferred:
                return itf
    import re
    for itf in ifaces:
        if re.search(r"(wan|ppp|internet|uplink|dsl)", str(itf.get("name") or ""), re.I):
            return itf
    candidates = [i for i in ifaces if i.get("oper") in (1, "up", None) and (i.get("in") or 0) + (i.get("out") or 0) > 0]
    return max(candidates, key=lambda i: (i.get("in") or 0) + (i.get("out") or 0)) if candidates else None
