"""WS-Discovery (SOAP-over-UDP): one multicast Probe, answers collected for a few seconds.

Answers are parsed defensively (64 KiB cap, DTD/entity declarations refused) and only accepted from
addresses inside the scanned ranges.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import uuid
import xml.etree.ElementTree as ET

from . import mcast
from .scrub import scrub

log = logging.getLogger("lanloom.scan.wsd")

WSD_GROUP = "239.255.255.250"
WSD_PORT = 3702
MAX_DATAGRAM = 64 * 1024
MAX_RESPONSES = 1024
MAX_TYPES = 8
MAX_TYPE_LEN = 120
MAX_XADDRS = 4
MAX_ENTRIES_PER_IP = 8

_FORBIDDEN_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)", re.IGNORECASE)

_PROBE = ('<?xml version="1.0" encoding="utf-8"?>'
          '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"'
          ' xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
          ' xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
          "<soap:Header>"
          "<wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>"
          "<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>"
          "<wsa:MessageID>urn:uuid:{mid}</wsa:MessageID>"
          "</soap:Header><soap:Body><wsd:Probe/></soap:Body></soap:Envelope>")


def build_probe(message_id: str | None = None) -> bytes:
    return _PROBE.format(mid=message_id or uuid.uuid4()).encode("utf-8")


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el) -> str:
    return " ".join("".join(ch if ch.isprintable() else " " for ch in (el.text or "")).split())


def parse_probe_match(data: bytes) -> dict | None:
    """Return {"types", "xaddrs", "endpoint"} for a ProbeMatch (or Hello) message, else None."""
    if not data or len(data) > MAX_DATAGRAM or _FORBIDDEN_XML.search(data):
        return None
    try:
        parser = ET.XMLParser()
        parser.feed(data)
        root = parser.close()
    except (ET.ParseError, ValueError, UnicodeError):
        return None
    match = None
    for el in root.iter():
        if _local(el.tag) in ("ProbeMatch", "Hello"):
            match = el
            break
    if match is None:
        return None
    types: list[str] = []
    xaddrs: list[str] = []
    endpoint = None
    for el in match.iter():
        name = _local(el.tag)
        if name == "Types":
            for t in _text(el).split():
                local = t.rsplit(":", 1)[-1][:MAX_TYPE_LEN]
                if local and local not in types and len(types) < MAX_TYPES:
                    types.append(scrub(local))
        elif name == "XAddrs":
            for u in _text(el).split():
                if u.lower().startswith(("http://", "https://")) and len(u) <= 300 and len(xaddrs) < MAX_XADDRS:
                    xaddrs.append(scrub(u))
        elif name == "Address" and endpoint is None:
            raw = _text(el)
            if raw.lower().startswith("urn:uuid:"):
                raw = raw[9:]
            try:
                endpoint = str(uuid.UUID(raw))
            except ValueError:
                endpoint = None
    return {"types": types, "xaddrs": xaddrs, "endpoint": endpoint}


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, collected):
        self.collected = collected

    def datagram_received(self, data, addr):
        if len(self.collected) >= MAX_RESPONSES or not isinstance(addr, tuple) or not addr:
            return
        try:
            entry = parse_probe_match(data)
        except Exception:
            entry = None
        if entry is None:
            return
        per = self.collected.setdefault(addr[0], [])
        if len(per) < MAX_ENTRIES_PER_IP and entry not in per:
            per.append(entry)

    def error_received(self, exc):
        pass


async def probe(duration_s: float = 3.0, networks=(), iface_ips=(), progress=None,
                target: tuple[str, int] = (WSD_GROUP, WSD_PORT)) -> tuple[dict, int]:
    """Returns ({ip: [entries]}, datagrams sent). Only senders inside `networks` are kept."""
    loop = asyncio.get_running_loop()
    networks = list(networks)
    collected: dict[str, list] = {}
    sent = 0
    transports = []
    try:
        for sock in mcast.query_sockets(iface_ips, ttl=2):
            try:
                t, _ = await loop.create_datagram_endpoint(lambda: _Proto(collected), sock=sock)
                transports.append(t)
            except OSError:
                sock.close()
        if transports:
            pkt = build_probe()
            for t in transports:
                try:
                    t.sendto(pkt, target)
                    sent += 1
                except OSError:
                    pass
            start = loop.time()
            while True:
                elapsed = loop.time() - start
                if elapsed >= duration_s:
                    break
                if progress:
                    progress(min(elapsed / duration_s, 1.0), len(collected))
                await asyncio.sleep(min(0.25, duration_s - elapsed))
    except OSError as exc:
        log.debug("wsd probe failed: %s", type(exc).__name__)
    finally:
        for t in transports:
            t.close()

    def in_scope(ip) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a.version == n.version and a in n for n in networks)

    return {ip: list(v) for ip, v in collected.items() if in_scope(ip)}, sent
