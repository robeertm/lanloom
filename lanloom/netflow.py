"""NetFlow / IPFIX receiver: the traffic of every LAN device as seen by a firewall or router, for the data paths view.

A router (the "exporter") sends flow records over UDP: NetFlow v5, NetFlow v9 (RFC 3954) or IPFIX (RFC 7011). Each record
says "source address, destination address, bytes". This module reads them and books the bytes per device and country
through ``DataPaths.book`` (see ``datapaths.py``). Only the country (looked up from the peer address by ``DataPaths``),
the device id and the byte count are stored; no addresses, ports or host names are kept, the same rule as for data paths.

Direction: LAN address -> outside address is upload of that device, outside -> LAN is download. LAN <-> LAN and transit
traffic is counted but not booked. Flows of addresses another source already books exactly (the Lanloom host itself) are
skipped, so nothing is counted twice.

Mind the delay: firewalls usually export a flow when the connection ENDS (or after an active timeout of some minutes), so
a long stream appears in the view late, and then in one piece.

Safety: only exporters on the allow list are accepted (an empty list accepts nobody), every parser step is bounds checked,
malformed or hostile packets are only counted and never raise, template tables are capped (512 templates per exporter,
128 fields per template). A data record whose template has not arrived yet cannot be read; it is counted as ``waiting``
(per data set) and the exporter's next template packet fixes that.

Sampling: an exporter that looks at only 1 of N packets says so (v5 header, the v9/IPFIX sampling interval in a record
or in options data, IPFIX packet interval/space); its byte counts are scaled up by that rate.
"""
from __future__ import annotations

import asyncio
import errno
import ipaddress
import logging
import socket
import struct
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("lanloom.netflow")

DEFAULT_PORT = 2055
UNKNOWN_DEVICE = "~unknown"
MAX_EXPORTERS = 8
MAX_TEMPLATES = 512
MAX_FIELDS = 128
MAX_FLOW_BYTES = 1 << 40            # one record never books more than 1 TiB (keeps sums far below the SQLite integer limit)
DROP_LOG_EVERY = 600.0              # at most one summary line about dropped packets per 10 minutes
MAX_LOGGED_SENDERS = 64
BIND_FAILED = "settings.netflow.bind_failed"

_IE_IN_BYTES, _IE_OUT_BYTES = 1, 23
_IE_SRC4, _IE_DST4, _IE_SRC6, _IE_DST6 = 8, 12, 27, 28
_IE_SAMPLING = 34                     # v9 SAMPLING_INTERVAL / IPFIX samplingInterval: 1 of N packets
_IE_PKT_INTERVAL, _IE_PKT_SPACE = 305, 306   # IPFIX: N packets selected, then M skipped -> rate (N + M) / N
_WANTED = frozenset((_IE_IN_BYTES, _IE_OUT_BYTES, _IE_SRC4, _IE_DST4, _IE_SRC6, _IE_DST6, _IE_SAMPLING,
                     _IE_PKT_INTERVAL, _IE_PKT_SPACE))
MAX_SAMPLING = 1 << 20
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_V5_HEADER, _V5_RECORD, _V5_MAX = 24, 48, 30


# ---------------------------------------------------------------------- templates
class Template:
    """Field list of a data set: ((information element or 0 = ignore, length or None = variable), ...)."""
    __slots__ = ("fields", "minlen", "options")

    def __init__(self, fields: tuple, options: bool = False):
        self.fields = fields
        self.minlen = sum(1 if ln is None else ln for _ie, ln in fields)
        self.options = options          # options data describes the exporter, never flows


_OPTIONS_MARK = Template((), options=True)


def _rate(vals: dict):
    """Sampling rate from the sampling fields of one record, None without them."""
    n = vals.get(_IE_SAMPLING)
    if n:
        return min(n, MAX_SAMPLING)
    sel, skip = vals.get(_IE_PKT_INTERVAL), vals.get(_IE_PKT_SPACE)
    if sel:
        return min(max(1.0, (sel + (skip or 0)) / sel), MAX_SAMPLING)
    return None


class TemplateCache:
    """Templates per (exporter, protocol version, observation domain, template id); capped per exporter."""

    def __init__(self):
        self._by_exporter: dict = {}
        self.sampling: dict = {}            # (exporter, version, domain) -> rate announced in options data

    def get(self, exporter: str, version: int, domain: int, tid: int) -> Optional[Template]:
        return self._by_exporter.get(exporter, {}).get((version, domain, tid))

    def put(self, exporter: str, version: int, domain: int, tid: int, tpl: Template) -> bool:
        table = self._by_exporter.setdefault(exporter, {})
        key = (version, domain, tid)
        if key not in table and len(table) >= MAX_TEMPLATES:
            return False
        table[key] = tpl
        return True

    def drop(self, exporter: str, version: int, domain: int, tid: int) -> None:
        self._by_exporter.get(exporter, {}).pop((version, domain, tid), None)

    def count(self, exporter: str) -> int:
        return len(self._by_exporter.get(exporter, {}))

    def forget(self, exporter: str) -> None:
        self._by_exporter.pop(exporter, None)
        self.sampling = {k: v for k, v in self.sampling.items() if k[0] != exporter}


def new_counters() -> dict:
    return {"malformed": 0, "waiting": 0, "refused": 0}


# ---------------------------------------------------------------------- parser
def _addr(raw: bytes):
    """4 or 16 bytes -> address object (IPv4-mapped IPv6 becomes IPv4), else None."""
    n = len(raw)
    if n == 4:
        return ipaddress.IPv4Address(raw)
    if n == 16:
        a = ipaddress.IPv6Address(raw)
        return a.ipv4_mapped or a
    return None


def _read_fields(buf: bytes, off: int, end: int, count: int, ipfix: bool):
    """Read `count` field specifiers; returns (fields, new offset) or None when they do not fit."""
    fields = []
    for _ in range(count):
        if off + 4 > end:
            return None
        ie, ln = struct.unpack_from(">HH", buf, off)
        off += 4
        if ipfix:
            if ie & 0x8000:                         # enterprise field: skipped, 4 bytes of enterprise number follow
                if off + 4 > end:
                    return None
                off += 4
                ie = 0
            if ln == 0xFFFF:
                ln = None
        fields.append((ie if ie in _WANTED else 0, ln))
    return tuple(fields), off


def _read_record(tpl: Template, buf: bytes, off: int, end: int):
    """One data record -> (new offset, src, dst, in bytes, out bytes, sampling rate) or None when it is cut off."""
    src4 = dst4 = src6 = dst6 = b_in = b_out = None
    samp: dict = {}
    for ie, ln in tpl.fields:
        if ln is None:                              # IPFIX variable length: 1 byte, or 255 + 2 bytes
            if off >= end:
                return None
            ln = buf[off]
            off += 1
            if ln == 255:
                if off + 2 > end:
                    return None
                ln = (buf[off] << 8) | buf[off + 1]
                off += 2
        if off + ln > end:
            return None
        if ie:
            raw = buf[off:off + ln]
            if ie in (_IE_SAMPLING, _IE_PKT_INTERVAL, _IE_PKT_SPACE):
                if 1 <= ln <= 8:
                    samp[ie] = int.from_bytes(raw, "big")
            elif ie == _IE_IN_BYTES or ie == _IE_OUT_BYTES:
                if 1 <= ln <= 8:
                    val = int.from_bytes(raw, "big")
                    if ie == _IE_IN_BYTES:
                        b_in = val
                    else:
                        b_out = val
            elif ie == _IE_SRC4 and ln == 4:
                src4 = raw
            elif ie == _IE_DST4 and ln == 4:
                dst4 = raw
            elif ie == _IE_SRC6 and ln == 16:
                src6 = raw
            elif ie == _IE_DST6 and ln == 16:
                dst6 = raw
        off += ln
    src, dst = src4 or src6, dst4 or dst6
    return off, (_addr(src) if src else None), (_addr(dst) if dst else None), b_in, b_out, _rate(samp)


def _read_data(tpl: Template, buf: bytes, off: int, end: int, flows: list, counters: dict,
               sampling: Optional[dict] = None, skey=None) -> None:
    """All records of one data set; trailing padding (shorter than a record) is ignored. Options records only update
    the sampling rate of their exporter; flow records are scaled up by their own or the announced rate."""
    if tpl.minlen <= 0:
        return
    while end - off >= tpl.minlen:
        rec = _read_record(tpl, buf, off, end)
        if rec is None:
            counters["malformed"] += 1
            return
        off, src, dst, b_in, b_out, rate = rec
        if tpl.options:
            if rate and sampling is not None:
                sampling[skey] = rate
            continue
        nbytes = b_in if b_in is not None else b_out
        if rate is None and sampling is not None:
            rate = sampling.get(skey)
        if nbytes is not None and rate and rate > 1:
            nbytes = int(nbytes * rate)
        if src is not None and dst is not None and nbytes is not None:
            flows.append((src, dst, nbytes))


def parse_v5(data: bytes, counters: dict) -> list:
    """NetFlow v5 -> [(src, dst, bytes)]."""
    if len(data) < _V5_HEADER:
        counters["malformed"] += 1
        return []
    count = struct.unpack_from(">H", data, 2)[0]
    if count == 0 or count > _V5_MAX or len(data) < _V5_HEADER + count * _V5_RECORD:
        counters["malformed"] += 1
        return []
    samp = struct.unpack_from(">H", data, 22)[0]       # 2 bits mode, 14 bits interval (1 of N packets)
    rate = (samp & 0x3FFF) if (samp >> 14) and (samp & 0x3FFF) > 1 else 1
    flows = []
    for i in range(count):
        o = _V5_HEADER + i * _V5_RECORD
        flows.append((ipaddress.IPv4Address(data[o:o + 4]), ipaddress.IPv4Address(data[o + 4:o + 8]),
                      struct.unpack_from(">I", data, o + 20)[0] * rate))
    return flows


def _parse_template_set(buf, off, end, exporter, ver, domain, cache: TemplateCache, counters, ipfix: bool) -> None:
    while end - off >= 4:
        tid, count = struct.unpack_from(">HH", buf, off)
        off += 4
        if tid < 256:
            counters["malformed"] += 1
            return
        if count == 0:
            if ipfix:                               # RFC 7011 template withdrawal
                cache.drop(exporter, ver, domain, tid)
                continue
            counters["malformed"] += 1
            return
        if count > MAX_FIELDS:
            cache.drop(exporter, ver, domain, tid)
            counters["refused"] += 1
            return                                  # cannot skip the rest of this set reliably
        got = _read_fields(buf, off, end, count, ipfix)
        if got is None:
            counters["malformed"] += 1
            return
        fields, off = got
        tpl = Template(fields)
        if tpl.minlen <= 0 or not cache.put(exporter, ver, domain, tid, tpl):
            counters["refused"] += 1


def _parse_options_set(buf, off, end, exporter, ver, domain, cache: TemplateCache, counters, ipfix: bool) -> None:
    """Options templates: only skipped safely; their id is remembered so their data sets are not 'waiting'."""
    while end - off >= 6:
        tid, a, b = struct.unpack_from(">HHH", buf, off)
        off += 6
        if ipfix:
            if a == 0:
                cache.drop(exporter, ver, domain, tid)
                continue
            if a > MAX_FIELDS:
                counters["refused"] += 1
                return
            got = _read_fields(buf, off, end, a, True)
            if got is None:
                counters["malformed"] += 1
                return
            fields, off = got
            fields = tuple((0, ln) if i < b else (ie, ln) for i, (ie, ln) in enumerate(fields))   # b scope fields
        else:                                       # v9: scope length and option length in bytes
            if off + a + b > end or a % 4 or b % 4 or (a + b) // 4 > MAX_FIELDS:
                counters["malformed"] += 1
                return
            scope = _read_fields(buf, off, off + a, a // 4, False)
            opts = _read_fields(buf, off + a, off + a + b, b // 4, False)
            if scope is None or opts is None:
                counters["malformed"] += 1
                return
            fields = tuple((0, ln) for _ie, ln in scope[0]) + opts[0]
            off += a + b
        tpl = Template(fields, options=True) if fields else _OPTIONS_MARK
        if tid >= 256 and not cache.put(exporter, ver, domain, tid, tpl):
            counters["refused"] += 1


def _parse_sets(buf, off, end, exporter, ver, domain, cache, counters, ipfix: bool) -> list:
    flows: list = []
    tpl_id, opt_id = (2, 3) if ipfix else (0, 1)
    while end - off >= 4:
        sid, length = struct.unpack_from(">HH", buf, off)
        if length < 4 or off + length > end:
            counters["malformed"] += 1
            break
        body, stop = off + 4, off + length
        if sid == tpl_id:
            _parse_template_set(buf, body, stop, exporter, ver, domain, cache, counters, ipfix)
        elif sid == opt_id:
            _parse_options_set(buf, body, stop, exporter, ver, domain, cache, counters, ipfix)
        elif sid >= 256:
            tpl = cache.get(exporter, ver, domain, sid)
            if tpl is None:
                counters["waiting"] += 1
            else:
                _read_data(tpl, buf, body, stop, flows, counters, cache.sampling, (exporter, ver, domain))
        off = stop
    return flows


def parse_v9(data: bytes, exporter: str, cache: TemplateCache, counters: dict) -> list:
    """NetFlow v9 (RFC 3954) -> [(src, dst, bytes)]; learns templates into `cache`."""
    if len(data) < 20:
        counters["malformed"] += 1
        return []
    domain = struct.unpack_from(">I", data, 16)[0]
    return _parse_sets(data, 20, len(data), exporter, 9, domain, cache, counters, False)


def parse_ipfix(data: bytes, exporter: str, cache: TemplateCache, counters: dict) -> list:
    """IPFIX (RFC 7011) -> [(src, dst, bytes)]; learns templates into `cache`."""
    if len(data) < 16:
        counters["malformed"] += 1
        return []
    length, domain = struct.unpack_from(">H", data, 2)[0], struct.unpack_from(">I", data, 12)[0]
    if length < 16 or length > len(data):
        counters["malformed"] += 1
        return []
    return _parse_sets(data, 16, length, exporter, 10, domain, cache, counters, True)


def parse_packet(data: bytes, exporter: str, cache: TemplateCache, counters: dict):
    """Any supported datagram -> (version or None, [(src, dst, bytes)]). Never raises."""
    try:
        if len(data) < 2:
            counters["malformed"] += 1
            return None, []
        ver = (data[0] << 8) | data[1]
        if ver == 5:
            return 5, parse_v5(data, counters)
        if ver == 9:
            return 9, parse_v9(data, exporter, cache, counters)
        if ver == 10:
            return 10, parse_ipfix(data, exporter, cache, counters)
    except Exception:
        log.debug("netflow parser error", exc_info=True)
    counters["malformed"] += 1
    return None, []


# ---------------------------------------------------------------------- settings helpers
def _norm_ip(value) -> Optional[str]:
    """Address text -> normalised text (IPv4-mapped becomes IPv4), None if it is not a plain address."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def parse_exporters(values) -> Optional[list]:
    """List of 1-8 exporter addresses -> normalised, de-duplicated list, or None if anything is invalid."""
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_EXPORTERS:
        return None
    out = []
    for v in values:
        text = _norm_ip(v)
        if text is None or "%" in v:
            return None
        ip = ipaddress.ip_address(text)
        if ip.is_unspecified or ip.is_multicast:
            return None
        if text not in out:
            out.append(text)
    return out


def parse_port(v) -> Optional[int]:
    """UDP port the container may bind without privileges (1024-65535), else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str) and v.strip().isdigit() and len(v.strip()) <= 5:
        v = int(v.strip())
    if isinstance(v, int) and 1024 <= v <= 65535:
        return v
    return None


# ---------------------------------------------------------------------- collector
class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, collector: "Collector"):
        self._c = collector

    def datagram_received(self, data, addr):
        try:
            self._c.handle(data, addr[0])
        except Exception:      # hostile payloads must never escape the datagram callback
            log.debug("netflow datagram error", exc_info=True)

    def error_received(self, exc):
        pass


class Collector:
    """Receives flow datagrams, filters by exporter, and books the bytes through `paths.book`."""

    def __init__(self, paths, device_of: Callable, lan_networks: Callable, skip_ips: Callable = lambda: set(),
                 clock: Callable = time.time, outside: Optional[Callable] = None):
        self._paths, self._device_of, self._lan_networks = paths, device_of, lan_networks
        self._skip_ips, self._clock, self._outside = skip_ips, clock, outside
        self._lock = threading.Lock()
        self._cache = TemplateCache()
        self._allowed: set = set()
        self._ex: dict = {}
        self._port = DEFAULT_PORT
        self._transport = None
        self._error: Optional[str] = None
        self._c = {"packets": 0, "flows": 0, "booked": 0, "bytes": 0, "dropped_foreign": 0, "skipped_local": 0,
                   "skipped_self": 0, **new_counters()}
        self._last_packet = self._last_flow = None
        self._drop_seen: set = set()
        self._drop_pending = 0
        self._drop_logged = None

    # -- configuration
    def configure(self, exporters: list, port: int) -> None:
        allowed = {t for t in (_norm_ip(e) for e in (exporters or [])) if t}
        with self._lock:
            for gone in self._allowed - allowed:
                self._cache.forget(gone)
                self._ex.pop(gone, None)
            for ip in allowed:
                self._ex.setdefault(ip, {"packets": 0, "flows": 0, "last": None, "version": None})
            self._allowed = allowed
            if isinstance(port, int) and not isinstance(port, bool):
                self._port = port

    @property
    def port(self) -> int:
        return self._port

    # -- socket
    async def start(self, host: str = "::", port: Optional[int] = None) -> None:
        """Open the UDP endpoint; a bind error is reported through stats (`error`), not raised."""
        self.stop()
        port = self._port if port is None else port
        loop = asyncio.get_running_loop()
        try:
            want6 = ipaddress.ip_address(host).version == 6
        except ValueError:
            want6 = True
        tries = [(socket.AF_INET6 if want6 else socket.AF_INET, host)]
        if host == "::":
            tries.append((socket.AF_INET, "0.0.0.0"))
        error = BIND_FAILED
        for family, addr in tries:
            sock = None
            try:
                sock = socket.socket(family, socket.SOCK_DGRAM)
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                except OSError:
                    pass
                sock.bind((addr, port))
                sock.setblocking(False)
                transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
            except OSError as exc:
                if sock is not None:
                    sock.close()
                if exc.errno in (errno.EADDRINUSE, errno.EACCES, errno.EPERM):
                    break                           # a real bind problem: the IPv4 fallback would not help
                continue
            with self._lock:
                self._transport, self._error = transport, None
                self._port = transport.get_extra_info("sockname")[1]
            return
        with self._lock:
            self._transport, self._error = None, error
        log.warning("netflow: cannot listen on UDP port %s", port)

    def stop(self) -> None:
        with self._lock:
            transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    # -- datagrams
    def handle(self, data: bytes, sender_ip: str) -> int:
        """One datagram from `sender_ip`; returns the number of booked flows. Never raises."""
        try:
            return self._handle(data, sender_ip)
        except Exception:
            log.debug("netflow handle error", exc_info=True)
            with self._lock:
                self._c["malformed"] += 1
            return 0

    def _handle(self, data: bytes, sender_ip: str) -> int:
        exporter = _norm_ip(sender_ip)
        now = self._clock()
        with self._lock:
            known = exporter is not None and exporter in self._allowed
            if known:
                self._c["packets"] += 1
                self._last_packet = now
                counters = new_counters()
                ver, flows = parse_packet(bytes(data), exporter, self._cache, counters)
                for k, v in counters.items():
                    self._c[k] += v
                ex = self._ex[exporter]
                ex["packets"] += 1
                ex["flows"] += len(flows)
                ex["last"] = now
                if ver:
                    ex["version"] = ver
                self._c["flows"] += len(flows)
                if flows:
                    self._last_flow = now
            else:
                self._c["dropped_foreign"] += 1
        if not known:
            self._note_drop(exporter or str(sender_ip)[:64], now)
            return 0
        return self._book(flows, now) if flows else 0

    def _note_drop(self, sender: str, now: float) -> None:
        with self._lock:
            self._drop_pending += 1
            if sender not in self._drop_seen and len(self._drop_seen) < MAX_LOGGED_SENDERS:
                self._drop_seen.add(sender)
                self._drop_logged, line = now, ("netflow: dropped packet from %s (not an allowed exporter)", sender)
                self._drop_pending = 0
            elif self._drop_logged is not None and now - self._drop_logged >= DROP_LOG_EVERY and self._drop_pending:
                line = ("netflow: dropped %d more packets from senders that are not allowed exporters", self._drop_pending)
                self._drop_logged, self._drop_pending = now, 0
            else:
                return
        log.warning(*line)

    # -- booking
    def _book(self, flows: list, now: float) -> int:
        try:
            nets = [n if not isinstance(n, str) else ipaddress.ip_network(n, strict=False) for n in self._lan_networks()]
        except Exception:
            nets = []
        try:
            skip = set(self._skip_ips() or ())
        except Exception:
            skip = set()
        memo: dict = {}
        booked = nbytes = local = own = 0
        for src, dst, size in flows:
            if str(src) in skip or str(dst) in skip:
                own += 1
                continue
            ks, kd = self._kind(src, nets, memo), self._kind(dst, nets, memo)
            size = min(size, MAX_FLOW_BYTES)
            if size <= 0 or ks[0] == kd[0] or ks[0] is None or kd[0] is None:
                local += 1                  # LAN <-> LAN, transit, special addresses, empty records
                continue
            lan, peer = (ks, dst) if ks[0] == "lan" else (kd, src)
            tx, rx = (size, 0) if ks[0] == "lan" else (0, size)
            try:
                ok = self._paths.book(lan[1], str(peer), tx, rx, now)
            except Exception:
                log.debug("netflow book error", exc_info=True)
                ok = False
            if ok:
                booked += 1
                nbytes += size
        with self._lock:
            self._c["booked"] += booked
            self._c["bytes"] += nbytes
            self._c["skipped_local"] += local
            self._c["skipped_self"] += own
        return booked

    def _kind(self, ip, nets: list, memo: dict):
        """('lan', device id) | ('out', None) | (None, None) for one address."""
        hit = memo.get(ip)
        if hit is not None:
            return hit
        try:
            dev = self._device_of(str(ip))
        except Exception:
            dev = None
        if dev or any(ip.version == n.version and ip in n for n in nets):
            res = ("lan", dev or UNKNOWN_DEVICE)
        elif self._is_outside(ip):
            res = ("out", None)
        elif ip.is_private:
            res = ("lan", UNKNOWN_DEVICE)
        else:
            res = (None, None)
        memo[ip] = res
        return res

    def _is_outside(self, ip) -> bool:
        if self._outside is not None:
            try:
                return bool(self._outside(ip))
            except Exception:
                return False
        if ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            return False
        return bool(ip.is_global or (ip.version == 4 and ip in _CGNAT))

    # -- status
    def stats(self) -> dict:
        with self._lock:
            c = self._c
            return {
                "packets": c["packets"], "flows": c["flows"], "booked": c["booked"], "bytes": c["bytes"],
                "dropped_foreign": c["dropped_foreign"], "malformed": c["malformed"], "waiting": c["waiting"],
                "refused": c["refused"], "skipped_local": c["skipped_local"], "skipped_self": c["skipped_self"],
                "last_packet": self._last_packet, "last_flow": self._last_flow,
                "exporters": {ip: dict(v) for ip, v in self._ex.items()},
                "listening": self._transport is not None, "port": self._port, "error": self._error,
            }
