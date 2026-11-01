"""mDNS / DNS-SD browsing (RFC 6762 / RFC 6763), read-only.

Queries are sent from an ephemeral port to 224.0.0.251:5353 with the QU bit
(unicast response requested). In addition the group is joined on port 5353
(shared with other responders via SO_REUSEADDR/SO_REUSEPORT) to catch
multicast answers; failure to join is tolerated.

Discovery order: service-type enumeration (`_services._dns-sd._udp.local`)
plus a fixed list of common types -> instances (PTR) -> SRV/TXT -> A.
Only answers that resolve to an address inside the scanned ranges are kept.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging

from . import dnsmsg, mcast
from .dnsmsg import T_A, T_PTR, T_SRV, T_TXT

log = logging.getLogger("lanloom.scan.mdns")

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICES_META = "_services._dns-sd._udp.local"

# Common service types (open standards / generic protocols). Others are learned from the enumeration.
COMMON_TYPES: tuple[str, ...] = (
    "_http._tcp", "_https._tcp", "_ipp._tcp", "_ipps._tcp", "_printer._tcp", "_pdl-datastream._tcp",
    "_uscan._tcp", "_scanner._tcp", "_smb._tcp", "_ssh._tcp", "_sftp-ssh._tcp", "_ftp._tcp",
    "_webdav._tcp", "_nfs._tcp", "_rfb._tcp", "_workstation._tcp", "_device-info._tcp", "_mqtt._tcp",
    "_rtsp._tcp", "_sip._udp", "_coap._udp", "_upnp._tcp",
)

MAX_TYPES = 128
MAX_INSTANCES = 512
MAX_HOSTS = 512
MAX_QUESTIONS_PER_PACKET = 12
MAX_PACKET = 9000


def _strip_local(name: str) -> str:
    name = name.rstrip(".")
    return name[:-6] if name.lower().endswith(".local") else name


def _is_service_type(name: str) -> bool:
    """`_x._tcp.local` / `_x._udp.local` (sub-types are ignored)."""
    parts = name.rstrip(".").lower().split(".")
    return (len(parts) == 3 and parts[0].startswith("_") and parts[1] in ("_tcp", "_udp")
            and parts[2] == "local")


def _reverse_v4(name: str) -> str | None:
    parts = name.rstrip(".").lower().split(".")
    if len(parts) == 6 and parts[4:] == ["in-addr", "arpa"]:
        try:
            return str(ipaddress.IPv4Address(".".join(reversed(parts[:4]))))
        except ValueError:
            return None
    return None


class MdnsCollector:
    """Pure state machine: feed received packets, ask what to query next, read results."""

    def __init__(self, types=COMMON_TYPES):
        self.types: set[str] = {t + ".local" for t in types}
        self.queried_types: dict[str, int] = {}
        self.instances: dict[str, str] = {}        # instance full name -> type full name
        self.srv: dict[str, dict] = {}             # instance -> {"port","target"}
        self.txt: dict[str, dict] = {}             # instance -> txt dict
        self.addrs: dict[str, set] = {}            # host name (lower) -> {ipv4}
        self.rev: dict[str, str] = {}              # ipv4 -> host name (from reverse PTR)
        self.src: dict[str, str] = {}              # instance or host -> source IP of the answer
        self.queried_detail: dict[str, int] = {}
        self.packets = 0
        self.bad_packets = 0

    # ------------------------------------------------------------- input
    def feed(self, data: bytes, addr) -> None:
        self.packets += 1
        try:
            msg = dnsmsg.parse_message(data)
        except dnsmsg.DNSError:
            self.bad_packets += 1
            return
        if not msg["response"]:
            return
        src = addr[0] if isinstance(addr, tuple) and addr else None
        for rec in msg["records"]:
            try:
                self._record(rec, src)
            except (TypeError, ValueError, AttributeError, KeyError):
                self.bad_packets += 1

    def _record(self, rec: dict, src) -> None:
        name = rec["name"].rstrip(".")
        lname = name.lower()
        rtype = rec["type"]
        data = rec["data"]
        if data is None:
            return
        if rtype == T_PTR:
            if lname == SERVICES_META:
                if isinstance(data, str) and _is_service_type(data) and len(self.types) < MAX_TYPES:
                    self.types.add(data.rstrip(".").lower())
                return
            ip = _reverse_v4(lname)
            if ip:
                self.rev.setdefault(ip, data.rstrip("."))
                return
            if _is_service_type(lname) and isinstance(data, str):
                inst = data.rstrip(".")
                if inst.lower().endswith("." + lname) and len(self.instances) < MAX_INSTANCES:
                    self.instances.setdefault(inst, lname)
                    self.types.add(lname)
                    if src:
                        self.src.setdefault(inst, src)
            return
        if rtype == T_SRV and isinstance(data, dict):
            if len(self.srv) < MAX_INSTANCES or name in self.srv:
                self.srv[name] = {"port": int(data["port"]), "target": data["target"].rstrip(".")}
                if src:
                    self.src.setdefault(name, src)
                # an SRV implies the instance even if the PTR was not seen
                parts = name.split(".")
                if len(parts) >= 4:
                    stype = ".".join(parts[-3:]).lower()
                    if _is_service_type(stype) and len(self.instances) < MAX_INSTANCES:
                        self.instances.setdefault(name, stype)
            return
        if rtype == T_TXT and isinstance(data, dict):
            if name in self.instances or name in self.srv or len(self.txt) < MAX_INSTANCES:
                self.txt[name] = data
            return
        if rtype == T_A and isinstance(data, str):
            if len(self.addrs) < MAX_HOSTS or lname in self.addrs:
                self.addrs.setdefault(lname, set()).add(data)
                if src:
                    self.src.setdefault(lname, src)

    # ------------------------------------------------------------- what to ask next
    def next_questions(self, repeat_base: bool = False) -> list[tuple[str, int]]:
        qs: list[tuple[str, int]] = []
        if repeat_base or not self.queried_types:
            qs.append((SERVICES_META, T_PTR))
        for t in sorted(self.types):
            n = self.queried_types.get(t, 0)
            if n == 0 or (repeat_base and n < 2):
                qs.append((t, T_PTR))
                self.queried_types[t] = n + 1
        for inst in list(self.instances):
            if inst not in self.srv and self.queried_detail.get(inst, 0) < 2:
                qs.append((inst, T_SRV))
                qs.append((inst, T_TXT))
                self.queried_detail[inst] = self.queried_detail.get(inst, 0) + 1
        for s in list(self.srv.values()):
            tgt = s["target"].lower()
            if tgt and tgt not in self.addrs and self.queried_detail.get(tgt, 0) < 2:
                qs.append((s["target"], T_A))
                self.queried_detail[tgt] = self.queried_detail.get(tgt, 0) + 1
        return qs

    # ------------------------------------------------------------- output
    def results(self, networks=None) -> dict[str, dict]:
        """{ip: {"services": [...], "hostnames": set()}} for addresses inside `networks`."""
        def in_scope(ip) -> bool:
            if networks is None:
                return True
            try:
                a = ipaddress.ip_address(ip)
            except ValueError:
                return False
            return any(a.version == n.version and a in n for n in networks)

        out: dict[str, dict] = {}

        def host(ip) -> dict:
            return out.setdefault(ip, {"services": [], "hostnames": set()})

        for inst, stype in self.instances.items():
            srv = self.srv.get(inst)
            ips: list[str] = []
            target = None
            if srv:
                target = srv["target"]
                ips = sorted(i for i in self.addrs.get(target.lower(), ()) if in_scope(i))
            if not ips:
                s = self.src.get(inst) or (self.src.get(target.lower()) if target else None)
                if s and in_scope(s):
                    ips = [s]
            if not ips:
                continue
            suffix = "." + stype
            label = inst[:-len(suffix)] if inst.lower().endswith(suffix) else inst
            entry = {"type": _strip_local(stype), "name": label[:128],
                     "port": srv["port"] if srv else 0, "txt": dict(self.txt.get(inst, {}))}
            for ip in ips:
                h = host(ip)
                if entry not in h["services"]:
                    h["services"].append(entry)
                if target:
                    h["hostnames"].add(target)
        for hname, ips in self.addrs.items():
            for ip in ips:
                if in_scope(ip) and ip in out:
                    out[ip]["hostnames"].add(hname)
        for ip, hname in self.rev.items():
            if in_scope(ip):
                host(ip)["hostnames"].add(hname)
        return out


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, feed):
        self._feed = feed

    def datagram_received(self, data, addr):
        if len(data) <= MAX_PACKET:
            self._feed(data, addr)

    def error_received(self, exc):
        pass


def _packets(questions, unicast: bool) -> list[bytes]:
    out = []
    for i in range(0, len(questions), MAX_QUESTIONS_PER_PACKET):
        try:
            out.append(dnsmsg.build_query(questions[i:i + MAX_QUESTIONS_PER_PACKET], unicast=unicast))
        except dnsmsg.DNSError:
            continue
    return out


async def browse(duration: float = 3.0, networks=None, types=COMMON_TYPES,
                 target: tuple[str, int] = (MDNS_GROUP, MDNS_PORT), join: bool = True,
                 iface_ips=(), progress=None) -> tuple[dict, int]:
    """Browse for `duration` seconds. Returns (results by ip, number of packets sent)."""
    loop = asyncio.get_running_loop()
    col = MdnsCollector(types)
    transports = []
    senders = []
    sent = 0
    try:
        for qsock in mcast.query_sockets(iface_ips, ttl=255):
            try:
                tq, _ = await loop.create_datagram_endpoint(lambda: _Proto(col.feed), sock=qsock)
            except OSError:
                qsock.close()
                continue
            transports.append(tq)
            senders.append(tq)
        if join:
            lsock = mcast.listen_socket(MDNS_GROUP, MDNS_PORT, iface_ips)
            if lsock is not None:
                try:
                    tl, _ = await loop.create_datagram_endpoint(lambda: _Proto(col.feed), sock=lsock)
                    transports.append(tl)
                except OSError:
                    lsock.close()
        start = loop.time()
        repeated = False
        while senders:
            elapsed = loop.time() - start
            if elapsed >= duration:
                break
            repeat = not repeated and elapsed >= min(1.0, duration / 3)
            if repeat:
                repeated = True
            qs = col.next_questions(repeat_base=repeat)
            for pkt in _packets(qs, unicast=True):
                for tq in senders:
                    try:
                        tq.sendto(pkt, target)
                        sent += 1
                    except OSError:
                        pass
            if progress:
                progress(min(elapsed / duration, 1.0), len(col.instances))
            await asyncio.sleep(min(0.25, max(0.0, duration - elapsed)))
    except OSError as exc:
        log.debug("mdns browse failed: %s", type(exc).__name__)
    finally:
        for t in transports:
            t.close()
    return col.results(networks), sent
