"""Application core: inventory, scans, presence checks, live sources and the 1-second live loop."""
import asyncio
import ipaddress
import json
import logging
import time
from typing import Optional
from urllib.parse import urlparse

from . import classify, live
from .crypto import Vault
from .store import Store

log = logging.getLogger("lanloom")

PRESENCE_INTERVAL = 120.0
FLOW_KEEP = 3600.0
DEFAULT_SETTINGS = {
    "rescan_hours": 6,          # 0 = never; automatic rescans are always gentle
    "rescan_cidrs": [],
    "update_check": True,
    "language": None,
    "setup_done": False,
    "uplink_iface": "",
}

INTEGRATION_FIELDS = {
    # kind -> (plain config fields, secret fields)
    "hub": (("url", "allow_self_signed"), ("token",)),
    "mqtt": (("host", "port", "username", "tls", "allow_self_signed"), ("password",)),
    "snmp": (("host", "version", "user", "auth_proto", "priv_proto", "uplink"), ("community", "auth_key", "priv_key")),
}


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback and not addr.is_link_local


class Core:
    def __init__(self, cfg, store: Store, vault: Optional[Vault]):
        self.cfg = cfg
        self.store = store
        self.vault = vault
        self.demo = None
        self.scan_state = {"state": "idle"}
        self._scan_task: Optional[asyncio.Task] = None
        self._tasks: list = []
        self._source_tasks: dict = {}      # integration id or "host" -> task
        self.source_status: dict = {}      # key -> {"ok","key","t"}
        self.cadence: dict = {}            # source key -> live.Cadence
        self.quant = live.Quantizer()
        self.ring = live.Ring(600)
        self.clients: set = set()
        self.ip_to_device: dict = {}
        self.self_ip: Optional[str] = None
        self.gateway_ip: Optional[str] = None
        self.presence: dict = {}           # device id -> last confirmed alive (ts)
        self.flow_seen: dict = {}          # device id -> last ts with host flow bytes
        self.pending_bytes: dict = {}      # trace id -> [b0, b1]
        self.spread: dict = {}             # trace id -> [bytes0, bytes1, seconds left] (counters polled every N s)
        self.self_iface: dict = {}         # iface -> [tx/s, rx/s]
        self.hub: dict = {}                # integration id -> latest snapshot (+ state cadence)
        self.mqtt: dict = {}               # integration id -> {"topics": {root: [msgs/s, bytes/s, last]}, "t"}
        self.snmp: dict = {}               # integration id -> latest snmp frame
        self.topic_order: dict = {}        # integration id -> stable list of shown topic roots
        self.structure_version = 0
        self.last_frame: dict = {}
        self._refresh_index()

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        for key in DEFAULT_SETTINGS:
            out[key] = self.store.get_setting(key, DEFAULT_SETTINGS[key])
        return out

    def update_settings(self, patch: dict) -> dict:
        allowed = {"rescan_hours": int, "update_check": bool, "language": (str, type(None)), "setup_done": bool,
                   "uplink_iface": str}
        for key, value in patch.items():
            if key in allowed and isinstance(value, allowed[key]):
                if key == "rescan_hours" and value not in (0, 1, 3, 6, 12, 24):
                    continue
                if key == "language" and value not in (None, "en", "de"):
                    continue
                self.store.set_setting(key, value)
        return self.settings()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.cfg.probe:
            return            # probe start of an update candidate: serve the UI only, no discovery, no sources
        if self.demo:
            self._tasks.append(asyncio.create_task(self.demo.run(self)))
        else:
            self._start_source("host", "host", {})
            for integ in self.integrations():
                if integ["enabled"]:
                    self._start_integration(integ)
            self._tasks.append(asyncio.create_task(self._presence_loop()))
            self._tasks.append(asyncio.create_task(self._rescan_loop()))
        self._tasks.append(asyncio.create_task(self._tick_loop()))

    async def stop(self) -> None:
        for task in list(self._tasks) + list(self._source_tasks.values()) + ([self._scan_task] if self._scan_task else []):
            task.cancel()
        for task in list(self._tasks) + list(self._source_tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._source_tasks.clear()

    # ------------------------------------------------------------------ inventory
    def _refresh_index(self) -> None:
        self.ip_to_device = {}
        for dev in self.store.devices():
            self.ip_to_device[dev["ip"]] = dev["id"]
            data = dev["data"]
            if data.get("is_self"):
                self.self_ip = dev["ip"]
            if data.get("is_gateway"):
                self.gateway_ip = dev["ip"]
        self.structure_version += 1

    def devices(self) -> list:
        out = []
        for dev in self.store.devices():
            out.append(self.device_view(dev))
        return out

    def device_view(self, dev: dict) -> dict:
        data = dev["data"]
        kind = dev.get("user_kind") or dev["kind"]
        title = dev.get("user_title") or dev["title"] or ""
        return {
            "id": dev["id"], "ip": dev["ip"], "mac": dev.get("mac"), "kind": kind, "auto_kind": dev["kind"],
            "title": title, "auto_title": dev["title"], "confidence": dev["confidence"],
            "certainty": data.get("certainty", "unknown"), "reasons": data.get("reasons", []),
            "hostnames": data.get("hostnames", []), "services": classify.services_of(data),
            "ports": data.get("ports", []), "mdns": data.get("mdns", []), "ssdp": data.get("ssdp", []),
            "sources": data.get("sources", []), "is_gateway": bool(data.get("is_gateway")), "is_self": bool(data.get("is_self")),
            "hidden": bool(dev["hidden"]), "first_seen": dev["first_seen"], "last_seen": dev["last_seen"],
            "status": self.device_status(dev),
        }

    def device_status(self, dev: dict) -> str:
        now = time.time()
        alive = max(self.presence.get(dev["id"], 0), dev["last_seen"] if dev.get("last_scan") == self._last_scan_id() else 0)
        if self.flow_seen.get(dev["id"], 0) > now - 30:
            return "up"
        if alive and now - alive < PRESENCE_INTERVAL * 3:
            return "up"
        if dev.get("last_scan") is not None and dev.get("last_scan") != self._last_scan_id():
            return "down"
        return "unknown" if not alive else "down"

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
        self._refresh_index()
        return self.device_view(self.store.device(device_id))

    def forget_device(self, device_id: str) -> bool:
        ok = self.store.execute("DELETE FROM devices WHERE id=?", (device_id,)).rowcount > 0
        self._refresh_index()
        return ok

    # ------------------------------------------------------------------ scanning
    def networks(self) -> list:
        if self.demo:
            return self.demo.networks()
        from . import scan
        try:
            return scan.detect_networks()
        except Exception:     # never let interface detection break the page
            log.exception("network detection failed")
            return []

    def start_scan(self, cidrs: list, deep: bool = False, consent: bool = False, automatic: bool = False) -> dict:
        if self.cfg.probe:
            raise ValueError("app.err.probe")     # a trial start never sends a packet into the LAN
        if self._scan_task and not self._scan_task.done():
            raise ValueError("scan.err.running")
        if not cidrs:
            raise ValueError("scan.err.no_network")
        if self.demo:
            plan = {"cidrs": list(cidrs), "deep": bool(deep)}
        else:
            from . import scan
            plan = scan.make_plan(list(cidrs), deep=bool(deep), consent=bool(consent))   # raises ValueError(reason_key)
        mode = "deep" if deep else "gentle"
        scan_id = self.store.execute(
            "INSERT INTO scans(started, mode, cidrs, status) VALUES(?,?,?,?)",
            (time.time(), mode, json.dumps(list(cidrs)), "running")).lastrowid
        self.scan_state = {"state": "running", "id": scan_id, "mode": mode, "cidrs": list(cidrs), "started": time.time(),
                           "phase": "neighbors", "done": 0, "total": 0, "found": 0, "automatic": automatic}
        self._scan_task = asyncio.create_task(self._run_scan(scan_id, plan))
        if not automatic:
            self.store.set_setting("rescan_cidrs", list(cidrs))
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
                result = await self.demo.scan(plan, progress)
            else:
                from . import scan
                result = await scan.run(plan, progress)
            count = self.ingest(result, scan_id)
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

    def ingest(self, result: dict, scan_id: Optional[int]) -> int:
        """Classify every host of a ScanResult and store it; returns the number of hosts."""
        hosts = result.get("hosts") or []
        with self.store.transaction():
            for host in hosts:
                if not host.get("ip"):
                    continue
                verdict = classify.classify(host)
                data = dict(host)
                data.update({"certainty": verdict["certainty"], "reasons": verdict["reasons"],
                             "runner_up": verdict["runner_up"]})
                dev_id = classify.device_id(host)
                # a device first seen by IP and now with a MAC keeps its user edits
                if dev_id.startswith("d-") and not dev_id.startswith("d-ip-"):
                    old = self.store.one("SELECT id, user_title, user_kind, hidden FROM devices WHERE id=?",
                                         ("d-ip-" + host["ip"].replace(".", "-").replace(":", "-"),))
                    if old:
                        self.store.execute("UPDATE devices SET id=? WHERE id=?", (dev_id, old["id"]))
                self.store.upsert_device({"id": dev_id, "ip": host["ip"], "mac": host.get("mac"), "data": data,
                                          "kind": verdict["kind"], "confidence": verdict["confidence"],
                                          "title": verdict["title"]}, scan_id)
                self.presence[dev_id] = time.time()
        self._refresh_index()
        return len(hosts)

    async def _rescan_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                hours = int(self.store.get_setting("rescan_hours", 6) or 0)
                cidrs = self.store.get_setting("rescan_cidrs", []) or []
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

        async def probe(dev: dict) -> None:
            ports = [p["port"] for p in dev["data"].get("ports") or [] if isinstance(p.get("port"), int)]
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

        await asyncio.gather(*(probe(d) for d in self.store.devices(include_hidden=False) if _is_private(d["ip"])))

    # ------------------------------------------------------------------ integrations
    def integrations(self) -> list:
        out = []
        for row in self.store.query("SELECT * FROM integrations ORDER BY id"):
            cfg = json.loads(row["config"])
            secrets_set = {f: bool(self.vault and self.vault.has("int:%d:%s" % (row["id"], f)))
                           for f in INTEGRATION_FIELDS.get(row["kind"], ((), ()))[1]}
            st = self.source_status.get("int:%d" % row["id"], {})
            out.append({"id": row["id"], "kind": row["kind"], "name": row["name"], "config": cfg, "secrets": secrets_set,
                        "enabled": bool(row["enabled"]), "status": st,
                        "grade": self._source_grade("int:%d" % row["id"])})
        return out

    @staticmethod
    def clean_integration(kind: str, config: dict, secrets: dict) -> tuple:
        if kind not in INTEGRATION_FIELDS:
            raise ValueError("integration.err.kind")
        plain_fields, secret_fields = INTEGRATION_FIELDS[kind]
        cfg = {k: config.get(k) for k in plain_fields if config.get(k) not in (None, "")}
        sec = {k: str(secrets.get(k)) for k in secret_fields if secrets.get(k) not in (None, "")}
        if kind == "hub":
            url = str(cfg.get("url") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("integration.err.url")
            cfg["url"] = url.rstrip("/")
        else:
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
        for k in ("allow_self_signed", "tls"):
            if k in cfg:
                cfg[k] = bool(cfg[k])
        return cfg, sec

    def _build_source(self, kind: str, cfg: dict, sec: dict):
        from . import sources
        if kind == "hub":
            return sources.Hub(cfg["url"], sec.get("token", ""), allow_self_signed=bool(cfg.get("allow_self_signed")))
        if kind == "mqtt":
            return sources.Mqtt(cfg["host"], port=cfg.get("port", 1883), username=cfg.get("username"),
                                password=sec.get("password"), tls=bool(cfg.get("tls")),
                                allow_self_signed=bool(cfg.get("allow_self_signed")), sys_topics=True)
        if kind == "snmp":
            return sources.Snmp(cfg["host"], version=cfg.get("version", "2c"), community=sec.get("community"),
                                user=cfg.get("user"), auth_key=sec.get("auth_key"), priv_key=sec.get("priv_key"),
                                auth_proto=cfg.get("auth_proto") or "sha", priv_proto=cfg.get("priv_proto") or "aes")
        raise ValueError("integration.err.kind")

    async def test_integration(self, kind: str, config: dict, secrets: dict, existing_id: Optional[int] = None) -> dict:
        cfg, sec = self.clean_integration(kind, config, secrets)
        if existing_id and self.vault:
            for field in INTEGRATION_FIELDS[kind][1]:
                if field not in sec:
                    stored = self.vault.get("int:%d:%s" % (existing_id, field))
                    if stored:
                        sec[field] = stored
        if self.demo:
            return {"ok": True, "key": "source.%s.ok" % ("hostnet" if kind == "host" else kind), "details": self.demo.test_details(kind)}
        try:
            src = self._build_source(kind, cfg, sec)
            ok, key, details = await asyncio.wait_for(src.test(), 25)
        except asyncio.TimeoutError:
            ok, key, details = False, "source.%s.timeout" % kind, {}
        except Exception as exc:     # sources raise SourceError with a reason key
            ok, key, details = False, getattr(exc, "key", "source.%s.error" % kind), {}
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

    def delete_integration(self, integ_id: int) -> bool:
        self._stop_source("int:%d" % integ_id)
        ok = self.store.execute("DELETE FROM integrations WHERE id=?", (integ_id,)).rowcount > 0
        if self.vault:
            self.vault.delete_prefix("int:%d:" % integ_id)
        for d in (self.hub, self.mqtt, self.snmp, self.topic_order):
            d.pop(integ_id, None)
        self.source_status.pop("int:%d" % integ_id, None)
        self.structure_version += 1
        return ok

    def _start_integration(self, integ: dict) -> None:
        sec = {}
        if self.vault:
            for field in INTEGRATION_FIELDS[integ["kind"]][1]:
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
                self.source_status[key] = {"ok": False, "key": getattr(exc, "key", "source.%s.config" % kind), "t": time.time()}
                return
            await src.run(lambda ev: self.on_event(key, integ_id, ev))

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
        elif etype == "hub_snapshot":
            cad.tick(now)
            entry = self.hub.setdefault(integ_id, {"states": {}, "events": live.Cadence()})
            entry.update({"snapshot": ev, "t": now})
            self.structure_version += 1
        elif etype == "hub_state":
            cad.tick(now)
            entry = self.hub.setdefault(integ_id, {"states": {}, "events": live.Cadence()})
            entry["states"][ev.get("entity_id")] = (ev.get("state"), ev.get("t") or now)
            entry["events"].tick(now)
        elif etype == "mqtt":
            cad.tick(now)
            self._book_mqtt(integ_id, ev, now)
        elif etype == "snmp":
            cad.tick(now)
            self._book_snmp(integ_id, ev, now)

    def _add_bytes(self, trace: str, b0: int, b1: int) -> None:
        acc = self.pending_bytes.setdefault(trace, [0, 0])
        acc[0] += max(0, int(b0 or 0))
        acc[1] += max(0, int(b1 or 0))

    def _book_flows(self, flows: list, now: float) -> None:
        self_id = self.ip_to_device.get(self.self_ip) if self.self_ip else None
        if not self_id:
            self_id = "self"
        router_id = self.ip_to_device.get(self.gateway_ip) if self.gateway_ip else None
        for fl in flows:
            tx, rx = fl.get("tx"), fl.get("rx")
            if tx is None and rx is None:
                continue
            peer = fl.get("peer_ip") or ""
            dev_id = self.ip_to_device.get(peer)
            if dev_id and dev_id != self_id:
                self._add_bytes(flow_trace(self_id, dev_id), tx, rx)
                self.flow_seen[dev_id] = now
            elif router_id:
                self._add_bytes(lan_trace(router_id, self_id), rx, tx)     # router -> self is "a -> b"

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
        self.snmp[integ_id] = ev
        integ = self.store.one("SELECT config FROM integrations WHERE id=?", (integ_id,)) if integ_id else None
        cfg = json.loads(integ["config"]) if integ else {}
        host_dev = self.ip_to_device.get(cfg.get("host") or ev.get("host") or "")
        if not host_dev:
            return
        if host_dev == self.ip_to_device.get(self.gateway_ip or ""):
            itf = pick_uplink(ev.get("ifaces") or [], cfg.get("uplink") or self.store.get_setting("uplink_iface", ""))
            if itf and itf.get("in") is not None:
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
        for (trace, direction), r in self.quant.rates(now).items():
            rates.setdefault(trace, [0, 0])[direction] = round(r)
        from . import mapmodel
        grades, metrics = mapmodel.live_overlay(self, now, rates)
        sources = self.source_list()
        frame = {"t": round(now, 3), "q": self.quant.q, "dots": dots, "rates": rates, "grades": grades, "metrics": metrics,
                 "overall": {"grade": live.typical([s["grade"] for s in sources]), "sources": sources},
                 "structure": self.structure_version}
        self.last_frame = frame
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
