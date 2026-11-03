"""SSDP discovery (UPnP 1.x), read-only.

One M-SEARCH for `ssdp:all` (MX 2) is sent to 239.255.255.250:1900 and
repeated once; answers are collected for a few seconds. The LOCATION
description (XML) is fetched only from addresses inside the scanned private
ranges, with a 64 KiB cap and a 2 s timeout. XML with a DTD or entity
declarations is refused outright.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET

from . import mcast
from .httpget import http_get, parse_head
from .scrub import scrub

log = logging.getLogger("lanloom.scan.ssdp")

SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
DESC_CAP = 64 * 1024
DESC_TIMEOUT = 2.0
MAX_RESPONSES = 1024
MAX_DESCRIPTIONS = 64
MAX_FIELD = 160

_FORBIDDEN_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)", re.IGNORECASE)


def msearch(st: str = "ssdp:all", mx: int = 2) -> bytes:
    return ("M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {int(mx)}\r\n"
            f"ST: {st}\r\n"
            "USER-AGENT: lanloom-discovery/1 UPnP/1.1\r\n\r\n").encode("ascii")


def _clean(value, limit: int = MAX_FIELD) -> str | None:
    if value is None:
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(value)).split())
    return scrub(text)[:limit] or None


def parse_response(data: bytes) -> dict | None:
    """Parse an SSDP search response or NOTIFY. Returns lower-cased headers or None."""
    if not data or len(data) > 8192:
        return None
    status, headers = parse_head(data.split(b"\r\n\r\n", 1)[0])
    first = data.split(b"\r\n", 1)[0].upper()
    if not (status == 200 or first.startswith(b"NOTIFY ")):
        return None
    if "st" not in headers and "nt" not in headers:
        return None
    return headers


def safe_parse_xml(raw: bytes):
    """Parse untrusted XML. Refuses DTDs/entities. Returns the root element or None."""
    if not raw or len(raw) > DESC_CAP:
        return None
    if _FORBIDDEN_XML.search(raw):
        return None
    try:
        parser = ET.XMLParser()
        parser.feed(raw)
        return parser.close()
    except (ET.ParseError, ValueError, UnicodeError):
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_description(raw: bytes) -> dict | None:
    """Extract the root device's friendlyName/manufacturer/modelName/deviceType."""
    root = safe_parse_xml(raw)
    if root is None:
        return None
    device = None
    for el in root.iter():
        if _local(el.tag) == "device":
            device = el
            break
    if device is None:
        return None
    out = {"friendly_name": None, "manufacturer": None, "model": None, "device_type": None}
    keys = {"friendlyName": "friendly_name", "manufacturer": "manufacturer",
            "modelName": "model", "deviceType": "device_type"}
    for child in device:
        k = keys.get(_local(child.tag))
        if k and out[k] is None:
            out[k] = _clean(child.text)
    return out


def location_allowed(location: str, responder_ip: str, networks) -> tuple[str, int, str, bool] | None:
    """Return (ip, port, path, tls) when LOCATION points into the scanned ranges (IP literal only)."""
    try:
        u = urllib.parse.urlsplit(location.strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            return None
        ip = ipaddress.ip_address(u.hostname)
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None
    if ip.version != 4 or not any(ip in n for n in networks if n.version == 4):
        return None
    if str(ip) != responder_ip:
        return None
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return str(ip), port, path, u.scheme == "https"


def _st_rank(st: str) -> int:
    s = (st or "").lower()
    if ":device:" in s:
        return 0
    if s == "upnp:rootdevice":
        return 1
    if ":service:" in s:
        return 3
    return 2


class SsdpCollector:
    def __init__(self):
        self.responses: dict[str, dict] = {}   # ip -> {st_key: headers}
        self.packets = 0

    def feed(self, data: bytes, addr) -> None:
        self.packets += 1
        if len(self.responses) >= MAX_RESPONSES:
            return
        try:
            headers = parse_response(data)
        except (ValueError, UnicodeError):
            headers = None
        if headers is None or not isinstance(addr, tuple) or not addr:
            return
        ip = addr[0]
        st = headers.get("st") or headers.get("nt") or ""
        per = self.responses.setdefault(ip, {})
        if len(per) < 32:
            per.setdefault(st, headers)


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, feed):
        self._feed = feed

    def datagram_received(self, data, addr):
        self._feed(data, addr)

    def error_received(self, exc):
        pass


async def search(duration: float = 3.0, networks=(), target: tuple[str, int] = (SSDP_GROUP, SSDP_PORT),
                 fetch: bool = True, iface_ips=(), progress=None) -> tuple[dict, int]:
    """Returns ({ip: [ssdp entries]}, packets sent). Entries follow the ScanResult schema."""
    loop = asyncio.get_running_loop()
    networks = [n for n in networks]
    col = SsdpCollector()
    sent = 0
    transports = []
    try:
        for sock in mcast.query_sockets(iface_ips, ttl=2):
            try:
                t, _ = await loop.create_datagram_endpoint(lambda: _Proto(col.feed), sock=sock)
                transports.append(t)
            except OSError:
                sock.close()
        start = loop.time()
        pkt = msearch()
        if transports:
            for when in (0.0, min(1.0, duration / 3)):
                delay = start + when - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                for t in transports:
                    try:
                        t.sendto(pkt, target)
                        sent += 1
                    except OSError:
                        pass
            while True:
                elapsed = loop.time() - start
                if elapsed >= duration:
                    break
                if progress:
                    progress(min(elapsed / duration, 1.0), len(col.responses))
                await asyncio.sleep(min(0.25, duration - elapsed))
    except OSError as exc:
        log.debug("ssdp search failed: %s", type(exc).__name__)
    finally:
        for t in transports:
            t.close()

    def in_scope(ip) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a.version == n.version and a in n for n in networks)

    result: dict[str, list] = {}
    descriptions: dict[str, dict | None] = {}
    fetch_jobs: dict[str, tuple] = {}
    for ip, per in col.responses.items():
        if not in_scope(ip):
            continue
        for st, h in per.items():
            loc = h.get("location")
            if fetch and loc and loc not in fetch_jobs and len(fetch_jobs) < MAX_DESCRIPTIONS:
                allowed = location_allowed(loc, ip, networks)
                if allowed:
                    fetch_jobs[loc] = allowed

    async def _fetch(loc, job):
        ip, port, path, tls = job
        res = await http_get(ip, port, tls=tls, path=path, cap=DESC_CAP, timeout=DESC_TIMEOUT,
                             follow_redirects=False)
        if res is None or res.status != 200:
            descriptions[loc] = None
            return
        descriptions[loc] = parse_description(res.body)

    if fetch_jobs:
        await asyncio.gather(*(_fetch(loc, job) for loc, job in fetch_jobs.items()), return_exceptions=True)
        sent += len(fetch_jobs)

    for ip, per in col.responses.items():
        if not in_scope(ip):
            continue
        # one device usually answers ssdp:all several times (root device, uuid, device and service
        # types); group by LOCATION and keep the most descriptive search target
        groups: dict[str, list] = {}
        for st, h in per.items():
            groups.setdefault(h.get("location") or f"st:{st}", []).append((st, h))
        entries = []
        for key, items in groups.items():
            st, h = min(items, key=lambda it: _st_rank(it[0]))
            desc = descriptions.get(h.get("location") or "") or {}
            entries.append({"st": _clean(st) or "", "server": _clean(h.get("server")),
                            "friendly_name": desc.get("friendly_name"), "model": desc.get("model"),
                            "manufacturer": desc.get("manufacturer"), "device_type": desc.get("device_type")})
        if entries:
            result[ip] = entries
    return result, sent
