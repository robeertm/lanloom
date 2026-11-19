"""Services and web interfaces per device, the link probe and the certificate clock (E5).

What a device offers is read from what the scan, the banners, mDNS and UPnP already saw (``extract``): name, port, protocol,
the version **where the device names it itself** (the ``Server`` header, a DNS-SD text record, the UPnP ``SERVER`` line) and
whether the service is a web interface. Nothing is guessed from a port number alone, and nothing here logs in anywhere.

Every three minutes ``Services.round`` looks at the services it already knows - never at anything else, no sweeping:

* **web interface**: one ``GET /`` (the same read-only client as the banner). 2xx/3xx and any 4xx answer = alive; **401/403 =
  alive** (it wants a login, that is a healthy answer); **5xx = red** (it answers, but it is broken); no answer = a miss.
  Two misses in a row = **dead**: the link is no longer clickable. It heals itself with the next good answer, no message.
* **other service** (SSH, MQTT, file shares, ...): a TCP connect, and for ports that speak TLS from the first byte the handshake. The
  connection is closed at once. This is a look, not a login.
* **certificate** of every TLS service: the expiry date is read (also of a self-signed one), at most every six hours.
  Fewer than 30 days = hint, fewer than 14 = yellow, fewer than 3 = red, past = expired. The nightly report and the
  notification rules read the same list (``cert_list``).
* **availability**: every look is booked into the 5-minute slots as ``svc:<device>|<port>`` (the Stability list). A host
  that does not answer is the device's business, not the service's: nothing is probed and booked then.
* **Removed?**: a service that stayed away for 24 h of probing while its host answered is not a fault any more - it was
  probably switched off on purpose. It is shown as "Removed?", no longer booked as an outage, and looked at only once an
  hour so it comes back by itself. The operator can forget it.

Lanloom **never heals** anything: it only observes. Devices of the protection list keep their rules (printer raw ports
untouched, industrial hosts only on 80/443, passive hosts not at all). Only devices with a private address are probed.
"""
import asyncio
import json
import logging
import re
import time
from typing import Optional

from .scan import ports as ports_mod, protect as protect_mod
from .scan.httpget import http_get

log = logging.getLogger("lanloom")

PROBE_EVERY = 180.0            # seconds between rounds
SLOW_EVERY = 3600.0            # a "Removed?" service is looked at this often
DEAD_AFTER = 2                 # misses in a row until the link is no longer clickable
REMOVED_AFTER_S = 86400.0      # probing time with a missing service while the host answers
CERT_EVERY = 6 * 3600.0
MAX_ENDPOINTS = 400
CONCURRENCY = 4
TIMEOUT = 3.0
WARN_DAYS, YELLOW_DAYS, RED_DAYS = 30, 14, 3
SETTING = "service_probes"
CERT_ID_PREFIX = "svc:"

TLS_PORTS = frozenset({443, 465, 636, 853, 990, 993, 995, 2376, 4443, 5061, 5001, 6443, 7443, 8006, 8443, 8883, 9443})
WEB_SURE = frozenset({80, 443, 8080, 8443})
WEB_TYPES = {"_http._tcp": False, "_https._tcp": True, "_http-alt._tcp": False}
_SERVER_RX = re.compile(r"^([A-Za-z][A-Za-z0-9_.\-]{1,40})[/ ]v?([0-9][0-9A-Za-z.\-+_]{0,30})")
_VERSION_KEYS = ("version", "ver", "fw", "fwversion", "sw", "swversion", "firmware")
NON_WEB_NAMES = frozenset({"ssh", "ssh-alt", "telnet", "ftp", "ftps", "ftp-alt", "smb", "dns", "mqtt", "mqtts", "rsync", "remote-desktop", "vnc",
                           "printer-raw", "rtsp", "smtp", "smtps", "imap", "imaps", "pop3", "pop3s", "ldap", "ldaps", "db", "kv-store", "nfs",
                           "iscsi", "snmp", "ntp", "upnp", "sip", "sips", "amqp"})
_NOT_PRODUCTS = {"upnp", "http", "rtsp", "sip"}


# ---------------------------------------------------------------------- what a device offers
def parse_product(text, last: bool = False) -> tuple:
    """(product, version) from a ``Server`` header or a UPnP SERVER line, or (None, None). Only what the device says.
    A UPnP line reads "<system>/<version> UPnP/1.0 <product>/<version>": there the **last** product token is meant (``last``)."""
    found = []
    for token in re.split(r"[;,]|\s{2,}", str(text or "")):
        for part in token.split(" ") if "/" in token else [token]:
            m = _SERVER_RX.match(part.strip("() "))
            if m and m.group(1).lower() not in _NOT_PRODUCTS:
                found.append((m.group(1), m.group(2)))
    if not found:
        return None, None
    return found[-1] if last else found[0]


def _txt_version(txt) -> Optional[str]:
    if isinstance(txt, dict):
        for k in _VERSION_KEYS:
            for key, v in txt.items():
                if str(key).lower() == k and str(v).strip():
                    return str(v).strip()[:32]
    return None


def key_of(device_id: str, port: int) -> str:
    return "%s|%d" % (device_id, port)


def split_key(key: str) -> tuple:
    dev, _, port = key.rpartition("|")
    return dev, int(port)


def agent_listen(core, device_id: str) -> list:
    """The listening TCP services a host agent of this device reports (not the loopback ones): [{"port","proc"}]."""
    hub = getattr(core, "agents", None)
    if hub is None:
        return []
    out = []
    try:
        for aid, st in list(getattr(hub, "live", {}).items()):
            if hub.device_of(aid) != device_id:
                continue
            for r in st.get("listen") or []:
                if r.get("proto") == "tcp" and not r.get("local") and isinstance(r.get("port"), int):
                    out.append({"port": r["port"], "proc": str(r.get("proc") or "")[:15]})
    except Exception:
        log.exception("agent services could not be read")
    return out


def extract(data: dict, listen=()) -> list:
    """The services of one device record: [{"port","proto","name","web","tls","version","product","title","sources":[...]}],
    web interfaces first, then by port. Only open TCP ports, what the device announced on its own and what its host agent
    reports as listening (``listen``: [{"port","proc"}], see ``agent_listen``)."""
    by_port: dict = {}
    for p in data.get("ports") or []:
        port = p.get("port")
        if not isinstance(port, int) or p.get("proto", "tcp") != "tcp":
            continue
        banner = p.get("banner") if isinstance(p.get("banner"), dict) else None
        name = p.get("service") or ports_mod.service_name(port) or "tcp"
        candidate = port in ports_mod.WEB_PORTS
        web = (bool(banner) and name not in NON_WEB_NAMES) or (candidate and port in WEB_SURE)
        product, version = parse_product((banner or {}).get("server"))
        by_port[port] = {"port": port, "proto": "tcp", "name": name, "web": web, "candidate": candidate,
                         "tls": bool(ports_mod.WEB_PORTS.get(port)) if candidate else port in TLS_PORTS,
                         "version": version, "product": product, "title": (banner or {}).get("title"), "sources": ["scan"]}
    for m in data.get("mdns") or []:
        if not isinstance(m, dict) or not isinstance(m.get("port"), int):
            continue
        port, typ = m["port"], str(m.get("type") or "").lower()
        row = by_port.get(port)
        if row is None:
            row = by_port[port] = {"port": port, "proto": "tcp", "name": typ.lstrip("_").split(".")[0] or "tcp", "web": False,
                                   "candidate": False, "tls": port in TLS_PORTS, "version": None, "product": None, "title": None,
                                   "sources": []}
        if "mdns" not in row["sources"]:
            row["sources"].append("mdns")
        if typ in WEB_TYPES:
            row["web"], row["tls"] = True, WEB_TYPES[typ] or row["tls"]
        row["version"] = row["version"] or _txt_version(m.get("txt"))
        if not row["title"] and m.get("name"):
            row["title"] = str(m["name"])[:80]
    for l in listen or []:
        port = l["port"]
        row = by_port.get(port)
        if row is None:
            candidate = port in ports_mod.WEB_PORTS
            row = by_port[port] = {"port": port, "proto": "tcp", "name": l.get("proc") or ports_mod.service_name(port) or "tcp",
                                   "web": candidate and port in WEB_SURE, "candidate": candidate,
                                   "tls": bool(ports_mod.WEB_PORTS.get(port)) if candidate else port in TLS_PORTS,
                                   "version": None, "product": None, "title": None, "sources": []}
        if "agent" not in row["sources"]:
            row["sources"].append("agent")
    for s in data.get("ssdp") or []:
        if not isinstance(s, dict):
            continue
        product, version = parse_product(s.get("server"), last=True)
        if not version:
            continue
        for row in by_port.values():
            if row["web"] and not row["version"] and row["port"] in (80, 443, 8080, 8443, 5000, 5001, 49152):
                row["product"], row["version"] = row["product"] or product, version
                if "ssdp" not in row["sources"]:
                    row["sources"].append("ssdp")
                break
    out = sorted(by_port.values(), key=lambda r: (0 if r["web"] or r["candidate"] else 1, r["port"]))
    return out


def allowed_ports(klass: Optional[str]):
    """Predicate: may this port of a device of this protection class be looked at?"""
    if klass == "passive":
        return lambda port: False
    if klass == "printer":
        return lambda port: port not in protect_mod.PRINTER_RAW
    if klass == "industrial":
        return lambda port: port in protect_mod.INDUSTRIAL_WEB
    return lambda port: True


def tier(days) -> str:
    """The certificate clock: ok | hint (< 30 d) | warn (< 14 d, yellow) | danger (< 3 d, red) | expired | unknown."""
    if days is None:
        return "unknown"
    if days < 0:
        return "expired"
    if days < RED_DAYS:
        return "danger"
    if days < YELLOW_DAYS:
        return "warn"
    if days < WARN_DAYS:
        return "hint"
    return "ok"


def classify_status(status: int) -> str:
    """ok | auth | error | none for an HTTP status code (0 = no HTTP answer)."""
    if not status:
        return "none"
    if status in (401, 403):
        return "auth"
    if status >= 500:
        return "error"
    return "ok"


# ---------------------------------------------------------------------- the probes (network)
async def probe_web(ip: str, port: int, tls: bool, timeout: float = TIMEOUT, confirmed: bool = False) -> dict:
    """One ``GET /`` (redirects are not followed: a redirect is an answer). ``confirmed``: the port is known to speak HTTP, so a
    connection without an HTTP answer counts as a miss; on an unconfirmed candidate port it only means "not a web interface"."""
    t0 = time.monotonic()
    res = await http_get(ip, port, tls=tls, path="/", cap=2048, timeout=timeout, follow_redirects=False)
    ms = int((time.monotonic() - t0) * 1000)
    if res is None:
        reach = await probe_tcp(ip, port, min(timeout, 1.5))
        return {"kind": "nothttp" if reach["up"] and not confirmed else "miss", "status": 0, "ms": ms}
    if not res.status:
        return {"kind": "nothttp", "status": 0, "ms": ms}
    return {"kind": classify_status(res.status), "status": res.status, "ms": ms}


async def probe_tcp(ip: str, port: int, timeout: float = TIMEOUT) -> dict:
    t0 = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return {"up": False, "ms": int((time.monotonic() - t0) * 1000)}
    ms = int((time.monotonic() - t0) * 1000)
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 0.5)
    except (OSError, asyncio.TimeoutError):
        pass
    return {"up": True, "ms": ms}


async def read_cert(ip: str, port: int) -> Optional[dict]:
    from .nightly.certs import cert_info
    try:
        return await asyncio.to_thread(cert_info, ip, port)
    except Exception:
        return None


# ---------------------------------------------------------------------- state
class Services:
    """The services of the known devices, their probe state and the certificate list. One instance per core."""

    def __init__(self, core, clock=time.time):
        self.core = core
        self.clock = clock
        saved = core.store.get_setting(SETTING, {}) or {}
        self.state: dict = {k: v for k, v in (saved.get("state") or {}).items() if isinstance(v, dict)}
        self.last_round = float(saved.get("t") or 0)
        self.hidden: dict = {k: v for k, v in (saved.get("hidden") or {}).items()}   # forgotten key -> scan id at that time
        self.version = 0                      # rises when anything a reader shows changed
        self.running = False

    # ---------------------------------------------------------------- persistence
    def save(self) -> None:
        self.core.store.set_setting(SETTING, {"state": self.state, "t": self.last_round, "hidden": self.hidden})

    # ---------------------------------------------------------------- the endpoints
    def endpoints(self) -> list:
        """[{key, device, title, kind, ip, svc (extract row), probe (bool), klass}] of every service worth a look: what the last
        scan lists, plus what an earlier scan listed and nobody removed (it may be gone - that is what "Removed?" is for)."""
        core, out = self.core, []
        marks = core.protect_marks()
        for dev in core.store.devices(include_hidden=False):
            data = dev["data"]
            if data.get("is_self") or not dev["ip"]:
                continue
            klass = protect_mod.resolve(data, marks.get(dev["id"]))[0]
            allow = allowed_ports(klass)
            title = dev.get("user_title") or dev["title"] or dev["ip"]
            rows = {s["port"]: s for s in extract(data, agent_listen(core, dev["id"]))}
            for key in [k for k in list(self.hidden) if k.rpartition("|")[0] == dev["id"]]:
                if self.hidden[key] != dev.get("last_scan"):
                    self.hidden.pop(key, None)             # a newer scan lists it again
                else:
                    rows.pop(split_key(key)[1], None)
            for key, rec in self.state.items():            # remembered services the scan no longer lists
                d, port = split_key(key)
                if d == dev["id"] and port not in rows and isinstance(rec.get("svc"), dict) and key not in self.hidden:
                    rows[port] = rec["svc"]
            for port in sorted(rows, key=lambda p: (0 if rows[p].get("web") or rows[p].get("candidate") else 1, p)):
                if len(out) >= MAX_ENDPOINTS:
                    return out
                out.append({"key": key_of(dev["id"], port), "device": dev["id"], "title": title,
                            "kind": dev.get("user_kind") or dev["kind"] or "", "ip": dev["ip"], "svc": rows[port],
                            "probe": _private(dev["ip"]) and allow(port), "klass": klass})
        return out

    # ---------------------------------------------------------------- one round
    async def round(self, now: Optional[float] = None) -> int:
        """Look at every due service once. Returns the number of services looked at."""
        now = self.clock() if now is None else now
        if self.core.demo:
            from . import demo_services
            return demo_services.simulate(self, now)
        if self.running or not self.core.store.get_setting("service_probe", True):
            return 0
        self.running = True
        try:
            eps = self.endpoints()
            due, hostdown = [], 0
            for ep in eps:
                rec = self.state.setdefault(ep["key"], {})
                rec["svc"] = ep["svc"]
                if not ep["probe"]:
                    continue
                if not self._host_up(ep["device"]):
                    if rec.get("state") != "hostdown":
                        rec["state"], self.version = "hostdown", self.version + 1
                    hostdown += 1
                    continue
                every = SLOW_EVERY if rec.get("state") == "removed" else PROBE_EVERY
                if now - float(rec.get("t") or 0) >= every - 5:
                    due.append(ep)
            results = await self._probe_all(due)
            self.apply(due, results, now)
            self._forget_gone({ep["key"] for ep in eps})
            self.last_round = now
            self.save()
            self.book_certs(now)
            return len(due)
        finally:
            self.running = False

    def _host_up(self, device_id: str) -> bool:
        dev = self.core.store.device(device_id)
        return bool(dev) and self.core.device_status(dev) == "up"

    async def _probe_all(self, due: list) -> list:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(ep):
            svc = ep["svc"]
            async with sem:
                try:
                    if svc["web"] or svc["candidate"]:
                        res = await probe_web(ep["ip"], svc["port"], svc["tls"], confirmed=svc["web"])
                    else:
                        r = await probe_tcp(ep["ip"], svc["port"])
                        res = {"kind": "ok" if r["up"] else "miss", "status": 0, "ms": r["ms"]}
                    rec = self.state.get(ep["key"]) or {}
                    if svc["tls"] and res["kind"] in ("ok", "auth", "error", "nothttp") and (
                            not rec.get("cert") or time.time() - float(rec["cert"].get("t") or 0) >= CERT_EVERY):
                        res["cert"] = await read_cert(ep["ip"], svc["port"])
                except Exception:
                    log.exception("service probe failed (%s)", ep["key"])
                    res = {"kind": "miss", "status": 0, "ms": None}
                await asyncio.sleep(0.05)
                return res

        return list(await asyncio.gather(*(one(ep) for ep in due)))

    def apply(self, due: list, results: list, now: float, book: bool = True) -> None:
        """Book the answers: state per service, availability looks. Pure bookkeeping, no network."""
        answered = sum(1 for r in results if r["kind"] != "miss")
        if len(results) >= 3 and not answered:
            return                              # nobody answers at all: more likely Lanloom's own network than all of them
        avail = getattr(self.core, "availability", None) if book else None
        for ep, res in zip(due, results):
            rec = self.state.setdefault(ep["key"], {})
            before = (rec.get("state"), rec.get("web"), (rec.get("cert") or {}).get("days"))
            svc, kind = ep["svc"], res["kind"]
            span = min(2 * PROBE_EVERY, max(0.0, now - float(rec.get("t") or now))) if rec.get("t") else 0.0
            rec["t"], rec["ms"], rec["status"] = now, res.get("ms"), res.get("status") or None
            if svc["candidate"] and not svc["web"]:
                rec["web"] = kind in ("ok", "auth", "error")      # a candidate port: the answer decides
            elif svc["candidate"] and kind == "nothttp":
                rec["web"] = False
            else:
                rec["web"] = svc["web"]
            if "cert" in res:
                if res["cert"]:
                    rec["cert"] = dict(res["cert"], t=now)
                elif rec.get("cert"):
                    rec["cert"]["t"] = now              # unreadable this time: keep the last reading, look again later
            if kind in ("ok", "auth", "error", "nothttp"):
                rec.update(fails=0, ok_at=now, miss_s=0.0)
                rec["state"] = {"ok": "ok", "nothttp": "ok", "auth": "auth", "error": "error"}[kind]
                up = kind != "error"
            else:
                rec["fails"] = int(rec.get("fails") or 0) + 1
                rec["miss_s"] = float(rec.get("miss_s") or 0.0) + span
                if rec["fails"] >= DEAD_AFTER and rec["miss_s"] >= REMOVED_AFTER_S:
                    rec["state"] = "removed"
                else:
                    rec["state"] = "dead" if rec["fails"] >= DEAD_AFTER else "doubt"
                up = False
            if avail is not None and rec["state"] != "removed":
                avail.sample("svc:" + ep["key"], up, now)
            if before != (rec.get("state"), rec.get("web"), (rec.get("cert") or {}).get("days")):
                self.version += 1

    def _forget_gone(self, live: set) -> None:
        """The state of a device that is gone (forgotten, hidden) is dropped with it."""
        devices = {e.rpartition("|")[0] for e in live}
        for key in [k for k in self.state if k.rpartition("|")[0] not in devices]:
            self.state.pop(key, None)
            self.version += 1

    def forget(self, key: str) -> bool:
        """The operator says: this service is gone for good ("Removed?" confirmed). It comes back only when a newer scan lists it."""
        dev_id, _ = split_key(key)
        dev = self.core.store.device(dev_id)
        if dev is None:
            return False
        known = self.state.pop(key, None) is not None or any(e["key"] == key for e in self.endpoints())
        if known:
            self.hidden[key] = dev.get("last_scan")
            self.version += 1
            avail = getattr(self.core, "availability", None)
            if avail is not None:
                avail.forget("svc:" + key)
            self.save()
        return known

    def forget_device(self, device_id: str) -> None:
        for key in [k for k in self.state if k.rpartition("|")[0] == device_id]:
            self.state.pop(key, None)
            avail = getattr(self.core, "availability", None)
            if avail is not None:
                avail.forget("svc:" + key)
        for key in [k for k in self.hidden if k.rpartition("|")[0] == device_id]:
            self.hidden.pop(key, None)
        self.version += 1

    # ---------------------------------------------------------------- reading
    def view(self, ep: dict) -> dict:
        rec = self.state.get(ep["key"]) or {}
        svc = ep["svc"]
        web = rec["web"] if rec.get("web") is not None else (svc["web"] or svc["candidate"] and svc["port"] in WEB_SURE)
        state = rec.get("state") or ("unprobed" if not ep["probe"] else "unknown")
        scheme = "https" if svc["tls"] else "http"
        cert = rec.get("cert") or None
        out = {"key": ep["key"], "device": ep["device"], "title": ep["title"], "kind": ep["kind"], "ip": ep["ip"],
               "port": svc["port"], "proto": svc["proto"], "name": svc["name"], "web": bool(web), "tls": bool(svc["tls"]),
               "version": svc["version"], "product": svc["product"], "label": svc["title"],
               "state": state, "status": rec.get("status"), "ms": rec.get("ms"), "ok_at": rec.get("ok_at"),
               "probed_at": rec.get("t"), "sources": svc["sources"], "protected": ep["klass"]}
        out["clickable"] = bool(web) and state not in ("dead", "removed")
        out["url"] = "%s://%s%s/" % (scheme, ("[%s]" % ep["ip"]) if ":" in ep["ip"] else ep["ip"],
                                      "" if svc["port"] == (443 if svc["tls"] else 80) else ":%d" % svc["port"]) if web else None
        out["cert"] = None if not cert else {"days": cert.get("days"), "not_after": cert.get("not_after"), "issuer": cert.get("issuer"),
                                             "subject": cert.get("subject"), "self_signed": bool(cert.get("self_signed")),
                                             "tier": tier(cert.get("days")), "t": cert.get("t")}
        return out

    def listing(self) -> list:
        return [self.view(ep) for ep in self.endpoints()]

    def device_services(self, device_id: str) -> list:
        return [v for v in self.listing() if v["device"] == device_id]

    def summary(self, rows: Optional[list] = None) -> dict:
        rows = self.listing() if rows is None else rows
        web = [r for r in rows if r["web"]]
        certs = [r["cert"] for r in rows if r["cert"] and r["cert"]["days"] is not None]
        return {"services": len(rows), "web": len(web), "dead": sum(1 for r in web if r["state"] == "dead"),
                "error": sum(1 for r in rows if r["state"] == "error"),
                "removed": sum(1 for r in rows if r["state"] == "removed"),
                "certs": len(certs), "certs_soon": sum(1 for c in certs if c["tier"] in ("hint", "warn", "danger", "expired")),
                "certs_red": sum(1 for c in certs if c["tier"] in ("danger", "expired"))}

    def cert_list(self) -> list:
        """[{"id","name","days"}] for the nightly report and the notification rules (TLS services of the known devices)."""
        out = []
        for r in self.listing():
            if r["cert"] and r["cert"]["days"] is not None:
                out.append({"id": CERT_ID_PREFIX + r["key"], "name": "%s · %s" % (r["title"], _label(r)), "days": r["cert"]["days"]})
        return sorted(out, key=lambda c: (c["days"], c["name"]))[:40]

    def book_certs(self, now: float) -> None:
        """Hand certificates that are about to run out to the notification rules (once a day each, see notify.on_certs)."""
        notify = getattr(self.core, "notify", None)
        if notify is None or self.core.demo:
            return
        soon = [c for c in self.cert_list() if c["days"] < YELLOW_DAYS]
        if soon:
            try:
                notify.on_certs(soon, now)
            except Exception:
                log.exception("service certificates: notification failed")

    async def loop(self) -> None:
        await asyncio.sleep(2 if self.core.demo else 25)
        while True:
            try:
                n = await self.round()
            except asyncio.CancelledError:
                raise
            except Exception:
                n = 0
                log.exception("service round failed")
            await asyncio.sleep(2 if self.core.demo and not n else PROBE_EVERY)


def _label(r: dict) -> str:
    return "%s :%d" % (r["name"], r["port"])


def _private(ip: str) -> bool:
    from .core import _is_private
    return _is_private(ip)
