"""Opt-in MQTT output: a few of Lanloom's own figures as sensors of a home-automation hub (MQTT discovery).

Off by default. When an administrator switches it on, Lanloom connects to the MQTT broker they name (or, in add-on
mode, to the broker the add-on system offers) and publishes:

* one discovery message per entity under ``<discovery prefix>/<component>/<node id>/<object>/config`` (retained), so
  the hub creates the sensors by itself, all grouped under one device "Lanloom";
* the value of each entity under ``<base topic>/<node id>/<object>`` (retained, plain text, only when it changed and
  once every ten minutes as a refresh);
* ``online`` / ``offline`` under ``<base topic>/<node id>/status`` (the broker sends ``offline`` as the last will when
  Lanloom goes away without saying goodbye).

What is published are counts, a level, a connectivity flag and a time: devices total / online / offline / new since
yesterday, the internet line, open issues, the overall health, open vulnerability hints, when the last nightly report
was made and how many of its points are open. Never a device name, address, hardware address, person, place, tracker,
camera or anything a hub sent to Lanloom (privacy rules of the hub bridge, docs/HUB-BRIDGE.md#privacy).

Writing is confined in the same way as the read-only sources: the client sends only CONNECT, PUBLISH, PINGREQ and
DISCONNECT (``ALLOWED_PACKETS``; there is no SUBSCRIBE - nothing is read from the broker), and every PUBLISH topic must
be one of the topics computed from the settings for this one node (``Topics.allowed``). Both are checked when a packet
is built and again right before it is written. Lanloom still never writes to a device: the broker is a message relay
the administrator chose, and only Lanloom's own topics are touched - never a topic of another program.

Switching off (or changing the node id or a prefix) removes the entities again: empty retained messages on exactly the
topics that were published before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import ssl
import struct
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from . import __version__

log = logging.getLogger("lanloom")

CONNECT, CONNACK, PUBLISH, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 12, 13, 14
ALLOWED_PACKETS = frozenset({CONNECT, PUBLISH, PINGREQ, DISCONNECT})     # never SUBSCRIBE: nothing is read here
KEEPALIVE_S = 60
PING_EVERY_S = 30
ROUND_S = 30                    # figures are looked at every 30 s; only changes are sent
REFRESH_S = 600                 # every value once every ten minutes, in case the broker lost its retained messages
VULN_EVERY_S = 300              # the vulnerability count is the expensive one
TIMEOUT_S = 10
BACKOFF_S = (5, 15, 30, 60, 120, 300)
FAIL_NOTE_S = 600               # a connection failing this long shows on the overview and as an event
SETTING = "mqtt_out"
SECRET = "mqtt_out:password"
DEFAULT_DISCOVERY = "homeassistant"     # identifier: the hub's default MQTT discovery prefix (protocol value)
DEFAULTS = {"enabled": False, "broker": "manual", "host": "", "port": 1883, "username": "", "tls": False,
            "allow_self_signed": False, "discovery_prefix": DEFAULT_DISCOVERY, "base_topic": "lanloom", "node_id": "lanloom"}
BROKERS = ("manual", "addon")
LEVEL_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
NODE_RE = re.compile(r"[a-z0-9_]{1,40}")
MAX_PREFIX = 120
SUPERVISOR_URL = "http://supervisor/services/mqtt"     # add-on system API inside the add-on network (add-on mode only)
HEALTH = ("ok", "info", "warn", "danger")

# (object id, component, fixed discovery fields). Names come from the instance language (mqtt_out.entity.<object>).
ENTITIES = (
    ("devices_total", "sensor", {"state_class": "measurement", "icon": "mdi:devices"}),
    ("devices_online", "sensor", {"state_class": "measurement", "icon": "mdi:lan-connect"}),
    ("devices_offline", "sensor", {"state_class": "measurement", "icon": "mdi:lan-disconnect"}),
    ("devices_new", "sensor", {"state_class": "measurement", "icon": "mdi:new-box"}),
    ("internet", "binary_sensor", {"device_class": "connectivity", "payload_on": "ON", "payload_off": "OFF"}),
    ("open_issues", "sensor", {"state_class": "measurement", "icon": "mdi:alert-circle-outline"}),
    ("health", "sensor", {"device_class": "enum", "options": list(HEALTH), "icon": "mdi:heart-pulse"}),
    ("vulnerability_hints", "sensor", {"state_class": "measurement", "icon": "mdi:shield-alert-outline"}),
    ("nightly_report", "sensor", {"device_class": "timestamp", "icon": "mdi:file-document-outline"}),
    ("nightly_open_points", "sensor", {"state_class": "measurement", "icon": "mdi:format-list-checks"}),
)
OBJECTS = tuple(e[0] for e in ENTITIES)


class OutError(Exception):
    """A failure with a translation key (``mqtt_out.err.*``); the message never carries a secret."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(detail or key)
        self.key = key


# ---------------------------------------------------------------------------- settings and topics
def _prefix(raw, default: str) -> str:
    raw = str(raw if raw is not None else default).strip().strip("/")
    levels = raw.split("/")
    if not raw or len(raw) > MAX_PREFIX or not all(LEVEL_RE.fullmatch(lv) for lv in levels):
        raise ValueError("mqtt_out.err.topic")
    return raw


def validate(cur: dict, patch: dict) -> dict:
    """Merge a settings patch into the current settings; raises ValueError(key) on a bad value."""
    out = dict(DEFAULTS)
    out.update({k: cur[k] for k in DEFAULTS if k in cur})
    for k in ("enabled", "tls", "allow_self_signed"):
        if k in patch:
            if not isinstance(patch[k], bool):
                raise ValueError("mqtt_out.err.value")
            out[k] = patch[k]
    if "broker" in patch:
        if patch["broker"] not in BROKERS:
            raise ValueError("mqtt_out.err.value")
        out["broker"] = patch["broker"]
    if "host" in patch:
        host = str(patch["host"] or "").strip()
        if len(host) > 253 or any(c.isspace() or c in "/@#?" for c in host):
            raise ValueError("mqtt_out.err.host")
        out["host"] = host
    if "port" in patch:
        try:
            port = int(patch["port"])
        except (TypeError, ValueError):
            raise ValueError("mqtt_out.err.port")
        if isinstance(patch["port"], bool) or not 1 <= port <= 65535:
            raise ValueError("mqtt_out.err.port")
        out["port"] = port
    if "username" in patch:
        name = str(patch["username"] or "").strip()
        if len(name) > 128:
            raise ValueError("mqtt_out.err.value")
        out["username"] = name
    if "discovery_prefix" in patch:
        out["discovery_prefix"] = _prefix(patch["discovery_prefix"], DEFAULT_DISCOVERY)
    if "base_topic" in patch:
        out["base_topic"] = _prefix(patch["base_topic"], "lanloom")
    if "node_id" in patch:
        node = str(patch["node_id"] or "").strip().lower()
        if not NODE_RE.fullmatch(node):
            raise ValueError("mqtt_out.err.node")
        out["node_id"] = node
    if out["base_topic"] == out["discovery_prefix"]:
        raise ValueError("mqtt_out.err.topic")
    if out["enabled"] and out["broker"] == "manual" and not out["host"]:
        raise ValueError("mqtt_out.err.host")
    return out


class Topics:
    """Every topic one node may publish to. Nothing outside ``allowed`` can be written."""

    def __init__(self, settings: dict):
        self.discovery = settings["discovery_prefix"]
        self.base = settings["base_topic"]
        self.node = settings["node_id"]
        self.status = "%s/%s/status" % (self.base, self.node)
        self.state = {obj: "%s/%s/%s" % (self.base, self.node, obj) for obj in OBJECTS}
        self.config = {obj: "%s/%s/%s/%s/config" % (self.discovery, comp, self.node, obj) for obj, comp, _ in ENTITIES}
        self.allowed = frozenset([self.status, *self.state.values(), *self.config.values()])

    def check(self, topic: str) -> None:
        if topic not in self.allowed or "+" in topic or "#" in topic or topic.startswith("$"):
            raise OutError("mqtt_out.err.forbidden", "topic not on the allow-list of this node")


# ---------------------------------------------------------------------------- packets
def _length(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s) -> bytes:
    b = s if isinstance(s, bytes) else str(s).encode("utf-8")
    if len(b) > 65535:
        raise OutError("mqtt_out.err.value", "string too long")
    return struct.pack(">H", len(b)) + b


def build_packet(ptype: int, flags: int, body: bytes) -> bytes:
    if ptype not in ALLOWED_PACKETS:
        raise OutError("mqtt_out.err.forbidden", "packet type not allowed")
    return bytes([(ptype << 4) | (flags & 0x0F)]) + _length(len(body)) + body


def connect_packet(client_id: str, username: Optional[str], password: Optional[str], will_topic: str,
                   keepalive: int = KEEPALIVE_S) -> bytes:
    # clean session, last will "offline" on the own status topic (QoS 0, retained)
    flags = 0x02 | 0x04 | 0x20 | (0x80 if username else 0) | (0x40 if password else 0)
    head = _str("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive)
    body = _str(client_id) + _str(will_topic) + _str(b"offline")
    body += (_str(username) if username else b"") + (_str(password) if password else b"")
    return build_packet(CONNECT, 0, head + body)


def publish_packet(topics: Topics, topic: str, payload: bytes, retain: bool = True) -> bytes:
    topics.check(topic)
    return build_packet(PUBLISH, 0x01 if retain else 0x00, _str(topic) + payload)       # QoS 0


def _publish_topic(raw: bytes) -> str:
    """Topic of a built PUBLISH packet (for the second check right before writing)."""
    i, mult, n = 1, 1, 0
    while True:
        b = raw[i]
        n += (b & 0x7F) * mult
        i += 1
        if not b & 0x80:
            break
        mult *= 128
    tlen = struct.unpack(">H", raw[i:i + 2])[0]
    return raw[i + 2:i + 2 + tlen].decode("utf-8")


# ---------------------------------------------------------------------------- figures
def _iso(t) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def snapshot(core, now: Optional[float] = None, vulns: Optional[int] = None) -> dict:
    """object id -> payload text. Counts, a level, a flag and a time only - nothing that names a device or person."""
    s = core.overview.summary(now)
    dev = s.get("devices") or {}
    wan = (s.get("wan") or {}).get("state")
    rep = s.get("report") or {}
    latest = rep.get("latest") or {}
    issues = sum(1 for n in s.get("notes") or [] if n.get("level") in ("warn", "danger"))
    level = (s.get("health") or {}).get("level")
    out = {
        "devices_total": str(int(dev.get("total") or 0)),
        "devices_online": str(int(dev.get("online") or 0)),
        "devices_offline": str(int(dev.get("offline") or 0)),
        "devices_new": str(int(dev.get("new") or 0)),
        "internet": "ON" if wan == "up" else "OFF" if wan == "down" else "None",
        "open_issues": str(issues),
        "health": level if level in HEALTH else "None",
        "vulnerability_hints": "None" if vulns is None else str(int(vulns)),
        "nightly_report": _iso(latest.get("created")) or "None",
        "nightly_open_points": str(int(latest.get("open") or 0)) if latest else "None",
    }
    return out


def count_vulns(core, now: Optional[float] = None) -> Optional[int]:
    v = getattr(core, "vulns", None)
    if v is None:
        return None
    try:
        return len(v.open_findings(now))
    except Exception:
        return None


def discovery(topics: Topics, names: dict) -> dict:
    """config topic -> discovery payload (dict) for every entity."""
    device = {"identifiers": ["lanloom_%s" % topics.node], "name": "Lanloom", "manufacturer": "Lanloom",
              "model": "Network map", "sw_version": __version__}
    out = {}
    for obj, comp, extra in ENTITIES:
        cfg = {"name": names.get(obj) or obj.replace("_", " ").capitalize(),
               "unique_id": "lanloom_%s_%s" % (topics.node, obj), "state_topic": topics.state[obj],
               "availability_topic": topics.status, "payload_available": "online", "payload_not_available": "offline",
               "device": device, "origin": {"name": "Lanloom", "sw_version": __version__,
                                            "support_url": "https://github.com/robeertm/lanloom"}}
        cfg.update(extra)
        out[topics.config[obj]] = cfg
    return out


def entity_names(store) -> dict:
    """Entity names in the instance language (the hub shows them as they are)."""
    from .textfmt import languages
    lang = store.get_setting("language", None) or "en"
    if lang not in languages():
        lang = "en"
    from pathlib import Path
    names = {}
    for code in ("en", lang):
        try:
            data = json.loads((Path(__file__).resolve().parent / "i18n" / ("%s.json" % code)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for obj in OBJECTS:
            text = data.get("mqtt_out.entity.%s" % obj)
            if text:
                names[obj] = text
    return names


# ---------------------------------------------------------------------------- connection
class Publisher:
    """One connection to the broker. Writes only allow-listed packets to allow-listed topics."""

    def __init__(self, host: str, port: int, topics: Topics, username: str = "", password: str = "",
                 tls: bool = False, allow_self_signed: bool = False, timeout: float = TIMEOUT_S):
        self.host, self.port, self.topics = host, int(port), topics
        self._username, self._password = username or None, password or None
        self.tls, self.allow_self_signed, self.timeout = bool(tls), bool(allow_self_signed), timeout
        self.client_id = "lanloom-out-%s" % secrets.token_hex(4)
        self.reader = self.writer = None
        self.sent = {"packets": 0, "bytes": 0}
        self.last_write = 0.0

    def __repr__(self):
        return "Publisher(host=%r, port=%d, tls=%r)" % (self.host, self.port, self.tls)

    __str__ = __repr__

    def _ssl(self):
        if not self.tls:
            return None
        ctx = ssl.create_default_context()
        if self.allow_self_signed:          # explicit opt-in only
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def write(self, raw: bytes) -> None:
        ptype = raw[0] >> 4
        if ptype not in ALLOWED_PACKETS:
            raise OutError("mqtt_out.err.forbidden", "packet type not allowed")
        if ptype == PUBLISH:
            self.topics.check(_publish_topic(raw))
        self.writer.write(raw)
        await asyncio.wait_for(self.writer.drain(), self.timeout)
        self.sent["packets"] += 1
        self.sent["bytes"] += len(raw)
        self.last_write = time.monotonic()

    async def _read_packet(self) -> tuple:
        first = await self.reader.readexactly(1)
        n, mult = 0, 1
        for _ in range(4):
            b = (await self.reader.readexactly(1))[0]
            n += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        else:
            raise OutError("mqtt_out.err.protocol", "bad length")
        if n > 65536:
            raise OutError("mqtt_out.err.protocol", "unexpected large packet")
        body = await self.reader.readexactly(n) if n else b""
        return first[0] >> 4, body

    async def connect(self) -> None:
        ctx = self._ssl()
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=ctx, server_hostname=self.host if ctx else None),
                self.timeout)
        except ssl.SSLError as exc:
            raise OutError("mqtt_out.err.tls", type(exc).__name__)
        except (OSError, asyncio.TimeoutError) as exc:
            raise OutError("mqtt_out.err.unreachable", type(exc).__name__)
        try:
            await self.write(connect_packet(self.client_id, self._username, self._password, self.topics.status))
            ptype, body = await asyncio.wait_for(self._read_packet(), self.timeout)
            if ptype != CONNACK or len(body) < 2:
                raise OutError("mqtt_out.err.protocol", "no CONNACK")
            if body[1] in (4, 5):
                raise OutError("mqtt_out.err.auth", "broker refused the credentials")
            if body[1] != 0:
                raise OutError("mqtt_out.err.refused", "broker refused the connection")
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError) as exc:
            self._drop()
            raise OutError("mqtt_out.err.protocol", type(exc).__name__)
        except BaseException:
            self._drop()
            raise

    async def watch(self) -> None:
        """Reads what the broker sends (only PINGRESP is expected) until the connection ends."""
        while True:
            ptype, _ = await self._read_packet()
            if ptype not in (PINGRESP,):
                log.debug("mqtt output: ignoring packet type %d", ptype)

    async def publish(self, topic: str, payload, retain: bool = True) -> None:
        data = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        await self.write(publish_packet(self.topics, topic, data, retain))

    async def ping(self) -> None:
        await self.write(build_packet(PINGREQ, 0, b""))

    async def close(self) -> None:
        if self.writer is None:
            return
        try:
            await asyncio.wait_for(self.write(build_packet(DISCONNECT, 0, b"")), 2)
        except (OSError, OutError, asyncio.TimeoutError):
            pass
        self._drop()

    def _drop(self) -> None:
        try:
            if self.writer is not None:
                self.writer.close()
        except Exception:
            pass
        self.writer = None


def addon_broker(token: Optional[str] = None, url: str = SUPERVISOR_URL, opener=None) -> dict:
    """The broker the add-on system offers to this add-on (add-on mode only). Raises OutError."""
    token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise OutError("mqtt_out.err.no_addon_broker", "not running as an add-on")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        if opener is None:
            ctx = urllib.request.urlopen(req, timeout=TIMEOUT_S)
        else:                                   # tests hand in a stand-in
            ctx = opener(req, timeout=TIMEOUT_S)
        with ctx as resp:
            data = json.loads(resp.read(65536).decode("utf-8"))
    except Exception as exc:
        raise OutError("mqtt_out.err.no_addon_broker", type(exc).__name__)
    info = data.get("data") if isinstance(data, dict) else None
    if not isinstance(info, dict) or not info.get("host"):
        raise OutError("mqtt_out.err.no_addon_broker", "no broker offered")
    try:
        port = int(info.get("port") or (8883 if info.get("ssl") else 1883))
    except (TypeError, ValueError):
        raise OutError("mqtt_out.err.no_addon_broker", "bad port")
    return {"host": str(info["host"])[:253], "port": port, "tls": bool(info.get("ssl")),
            "username": str(info.get("username") or "")[:128], "password": str(info.get("password") or "")}


# ---------------------------------------------------------------------------- manager
class MqttOut:
    """Settings, status and the background loop of the MQTT output."""

    def __init__(self, core, store, vault, cfg, clock=time.time):
        self.core, self.store, self.vault, self.cfg, self.clock = core, store, vault, cfg, clock
        self._wake: Optional[asyncio.Event] = None          # created inside the running loop
        self._task: Optional[asyncio.Task] = None
        self._generation = 0
        self._cleanup: Optional[dict] = None    # settings whose topics must be emptied (switched off / renamed)
        self.status = {"state": "off", "error": None, "since": None, "published": 0, "last": None, "failing_since": None}
        self._vulns = (None, 0.0)
        self._announced = False
        self.opener = None                      # replaced in tests (add-on broker lookup)

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        raw = self.store.get_setting(SETTING, {}) or {}
        out = dict(DEFAULTS)
        out.update({k: raw[k] for k in DEFAULTS if k in raw})
        return out

    def addon_available(self) -> bool:
        return bool(getattr(self.cfg, "addon", False)) and bool(os.environ.get("SUPERVISOR_TOKEN"))

    def view(self) -> dict:
        st = self.settings()
        topics = Topics(st)
        now = self.clock()
        try:
            preview = snapshot(self.core, now, self._vuln_count(now))
        except Exception:
            preview = {}
        names = entity_names(self.store)
        status = dict(self.status)
        if self.cfg.demo and st["enabled"]:
            status.update(state="connected", demo=True, published=len(OBJECTS) * 2 + 1, since=now - 3 * 3600, last=now - 20)
        return {"settings": st, "password_set": self.vault.has(SECRET), "status": status,
                "addon": bool(getattr(self.cfg, "addon", False)), "addon_broker": self.addon_available(),
                "topics": {"status": topics.status, "state": topics.state, "config": topics.config},
                "entities": [{"object": obj, "component": comp, "name": names.get(obj, obj), "value": preview.get(obj),
                              "state_topic": topics.state[obj], "config_topic": topics.config[obj]}
                             for obj, comp, _ in ENTITIES]}

    def update(self, patch: dict) -> dict:
        cur = self.settings()
        new = validate(cur, patch)
        if new["broker"] == "addon" and new["enabled"] and not self.addon_available() and not self.cfg.demo:
            raise ValueError("mqtt_out.err.no_addon_broker")
        if "password" in patch:
            pw = str(patch["password"] or "")
            if len(pw) > 512:
                raise ValueError("mqtt_out.err.value")
            if pw:
                self.vault.put(SECRET, pw)
            else:
                self.vault.delete(SECRET)
        was_on = cur["enabled"]
        if was_on and (not new["enabled"] or Topics(cur).allowed != Topics(new).allowed):
            self._cleanup = cur                 # remove the entities of the old topics before anything else
        self.store.set_setting(SETTING, new)
        self._restart()
        return self.view()

    def _restart(self) -> None:
        self._generation += 1
        if self._wake is not None:
            self._wake.set()

    # ------------------------------------------------------------------ figures
    def _vuln_count(self, now: float) -> Optional[int]:
        val, at = self._vulns
        if at and now - at < VULN_EVERY_S:
            return val
        val = count_vulns(self.core, now)
        self._vulns = (val, now)
        return val

    # ------------------------------------------------------------------ connection details
    def _target(self, st: dict) -> dict:
        if st["broker"] == "addon":
            b = addon_broker(opener=self.opener)
            return dict(b, allow_self_signed=False)
        return {"host": st["host"], "port": st["port"], "tls": st["tls"], "username": st["username"],
                "password": self.vault.get(SECRET) or "", "allow_self_signed": st["allow_self_signed"]}

    def _publisher(self, st: dict) -> Publisher:
        t = self._target(st)
        return Publisher(t["host"], t["port"], Topics(st), username=t["username"], password=t["password"],
                         tls=t["tls"], allow_self_signed=t["allow_self_signed"])

    async def test(self, patch: Optional[dict] = None) -> dict:
        """Connect and disconnect once with the given (or saved) settings; nothing is published."""
        st = validate(self.settings(), dict(patch or {}, enabled=False))
        if self.cfg.demo:
            return {"ok": True, "demo": True}
        try:
            pub = self._publisher(st)
            if patch and patch.get("password"):
                pub._password = str(patch["password"])
            await pub.connect()
            await pub.close()
        except OutError as exc:
            return {"ok": False, "error": exc.key}
        return {"ok": True}

    # ------------------------------------------------------------------ background loop
    async def run(self) -> None:
        self._wake = asyncio.Event()
        failures = 0
        while True:
            self._wake.clear()
            gen = self._generation
            st = self.settings()
            if self._cleanup is not None and not self.cfg.demo:
                await self._remove(self._cleanup)
                self._cleanup = None
            elif self._cleanup is not None:
                self._cleanup = None
            if not st["enabled"] or self.cfg.demo:
                self._set("off")
                failures = 0
                await self._wake.wait()
                continue
            try:
                await self._session(st, gen)
                failures = 0
            except asyncio.CancelledError:
                raise
            except OutError as exc:
                failures += 1
                self._fail(exc.key)
            except Exception as exc:            # never let the loop die; the reason is in the status, not the secret
                failures += 1
                log.warning("mqtt output: %s", type(exc).__name__)
                self._fail("mqtt_out.err.protocol")
            if gen != self._generation:
                continue
            delay = BACKOFF_S[min(max(failures - 1, 0), len(BACKOFF_S) - 1)] if failures else 1
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def _session(self, st: dict, gen: int) -> None:
        self._set("connecting")
        pub = self._publisher(st)
        await pub.connect()
        watcher = asyncio.ensure_future(pub.watch())
        try:
            topics = pub.topics
            for topic, payload in discovery(topics, entity_names(self.store)).items():
                await pub.publish(topic, json.dumps(payload, separators=(",", ":"), sort_keys=True))
            await pub.publish(topics.status, "online")
            self._set("connected")
            self._recovered()
            last: dict = {}
            refreshed = 0.0
            while gen == self._generation:
                now = self.clock()
                values = snapshot(self.core, now, self._vuln_count(now))
                full = now - refreshed >= REFRESH_S
                for obj, val in values.items():
                    if full or last.get(obj) != val:
                        await pub.publish(topics.state[obj], val)
                        self.status["published"] += 1
                        self.status["last"] = now
                last = values
                if full:
                    refreshed = now
                if time.monotonic() - pub.last_write >= PING_EVERY_S:
                    await pub.ping()
                wake = asyncio.ensure_future(self._wake.wait())
                done, _ = await asyncio.wait({watcher, wake}, timeout=ROUND_S, return_when=asyncio.FIRST_COMPLETED)
                if not wake.done():
                    wake.cancel()
                if watcher in done:
                    exc = watcher.exception()
                    raise OutError("mqtt_out.err.lost", type(exc).__name__ if exc else "closed")
                if wake in done:
                    self._wake.clear()
        finally:
            # a clean DISCONNECT suppresses the last will, so say "offline" first (settings changed, Lanloom stops)
            watcher.cancel()
            if pub.writer is not None and self.status["state"] == "connected":
                try:
                    await asyncio.wait_for(pub.publish(pub.topics.status, "offline"), 2)
                except (OSError, OutError, asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            await pub.close()

    async def _remove(self, st: dict) -> None:
        """Empty retained messages on every topic of the given settings: the hub drops the entities."""
        try:
            pub = self._publisher(st)
            await pub.connect()
            try:
                topics = pub.topics
                for topic in list(topics.config.values()) + list(topics.state.values()) + [topics.status]:
                    await pub.publish(topic, b"")
            finally:
                await pub.close()
        except Exception as exc:                # the broker may be gone; a later switch-on overwrites the configs
            log.info("mqtt output: removing the old entities failed (%s)", getattr(exc, "key", type(exc).__name__))

    # ------------------------------------------------------------------ status, overview note, event
    def _set(self, state: str) -> None:
        if self.status["state"] != state:
            self.status["since"] = self.clock()
        self.status["state"] = state
        if state in ("connected", "off"):
            self.status["error"] = None
            self.status["failing_since"] = None

    def _fail(self, key: str) -> None:
        now = self.clock()
        if self.status["state"] != "failed":
            self.status["since"] = now
        self.status.update(state="failed", error=key)
        if not self.status.get("failing_since"):
            self.status["failing_since"] = now
        if now - self.status["failing_since"] >= FAIL_NOTE_S and not self._announced:
            self._announced = True
            notify = getattr(self.core, "notify", None)
            if notify is not None:
                try:
                    notify.emit("mqtt_out_down", "mqtt_out", {"reason_key": key}, t=now)
                except Exception:
                    log.exception("mqtt output: event failed")

    def _recovered(self) -> None:
        if self._announced:
            self._announced = False
            notify = getattr(self.core, "notify", None)
            if notify is not None:
                try:
                    notify.emit("mqtt_out_down", "mqtt_out", {}, resolve=True)
                except Exception:
                    log.exception("mqtt output: event failed")

    def failing_long(self, now: float) -> bool:
        since = self.status.get("failing_since")
        return self.status.get("state") == "failed" and bool(since) and now - since >= FAIL_NOTE_S


def overview_note(core, now: float) -> list:
    out_ = getattr(core, "mqtt_out", None)
    if out_ is None or not out_.failing_long(now):
        return []
    return [{"id": "mqtt-out", "level": "warn", "key": "ov.note.mqtt_out_failed", "sub": "ov.note.mqtt_out_failed.sub",
             "vars": {"reason_key": out_.status.get("error") or "", "age_dur": round(now - out_.status["failing_since"])},
             "link": "#/settings/mqtt"}]


def seed_demo(store) -> None:
    """Demo data: the output switched on towards a sample broker (no connection is made in the demo)."""
    store.set_setting(SETTING, dict(DEFAULTS, enabled=True, host="192.0.2.10", username="lanloom"))


def _enabled(core) -> bool:
    out_ = getattr(core, "mqtt_out", None)
    try:
        return out_ is not None and bool(out_.settings()["enabled"])
    except Exception:
        return False


def _register() -> None:
    from .notify.events import make, register
    from .overview import register_note
    register(make("mqtt_out_down", "connections", 33, "warning", recovery=True, available=_enabled))
    register_note(overview_note)


_register()
