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

Templates per exporter: v9 and IPFIX describe their records in templates, kept per (exporter, version, observation domain,
template id), so two exporters with the same template id never mix. Over UDP an exporter repeats its templates now and then;
a template that is not repeated expires (RFC 7011 section 8.4): after one hour at first, then after three times the longest
gap between two repeats seen for it (at most six hours). Its records then wait for the next template instead of being read
with a layout that may have changed. A NetFlow v9 exporter whose uptime jumps back has restarted: its templates of that
observation domain are dropped before the packet is read. Options templates and their records (they describe the exporter,
not flows) are skipped; only a sampling rate announced in them is used.

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
MAX_REFUSED_SHOWN = 8                # senders that are not allowed, shown with "Allow" in the settings (flow packets only)
REFUSED_SHOWN_FOR = 24 * 3600.0
TEMPLATE_LIFE = 3600.0               # a template not repeated for this long expires (until a repeat gap is known)
TEMPLATE_LIFE_MAX = 6 * 3600.0       # learned lifetime: 3 x the longest repeat gap, never longer than this
SAMPLING_LIFE = TEMPLATE_LIFE_MAX    # a sampling rate from options data is forgotten when it is not announced again
SWEEP_EVERY = 60.0                   # expired templates are swept at most once a minute
V9_RESTART_BACK_MS = 60_000          # v9 uptime that goes back by more than this: the exporter restarted
_V9_WRAP_MS = 1 << 32
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
    __slots__ = ("fields", "minlen", "options", "seen", "gap", "life")

    def __init__(self, fields: tuple, options: bool = False):
        self.fields = fields
        self.minlen = sum(1 if ln is None else ln for _ie, ln in fields)
        self.options = options          # options data describes the exporter, never flows
        self.seen = None                # when the exporter last sent (or repeated) this template
        self.gap = 0.0                  # longest gap between two repeats seen
        self.life = TEMPLATE_LIFE

    def expired(self, now) -> bool:
        return now is not None and self.seen is not None and now - self.seen > self.life


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
    """Templates per (exporter, protocol version, observation domain, template id); capped per exporter, with a lifetime.

    ``now`` is optional everywhere: without it (direct parser calls) templates never expire."""

    def __init__(self):
        self._by_exporter: dict = {}
        self.sampling: dict = {}            # (exporter, version, domain) -> (rate announced in options data, when)
        self.uptime: dict = {}              # (exporter, 9, domain) -> last v9 sysUptime in ms (restart detection)
        self.expired: dict = {}             # exporter -> templates that expired
        self.restarts: dict = {}            # exporter -> restarts detected

    def get(self, exporter: str, version: int, domain: int, tid: int, now=None) -> Optional[Template]:
        table = self._by_exporter.get(exporter)
        if not table:
            return None
        key = (version, domain, tid)
        tpl = table.get(key)
        if tpl is not None and tpl.expired(now):
            del table[key]
            self.expired[exporter] = self.expired.get(exporter, 0) + 1
            return None
        return tpl

    def put(self, exporter: str, version: int, domain: int, tid: int, tpl: Template, now=None) -> bool:
        table = self._by_exporter.setdefault(exporter, {})
        key = (version, domain, tid)
        old = table.get(key)
        if old is not None and old.expired(now):
            del table[key]
            self.expired[exporter] = self.expired.get(exporter, 0) + 1
            old = None
        if old is None and len(table) >= MAX_TEMPLATES:
            if not self.sweep(now, exporter) or len(table) >= MAX_TEMPLATES:
                return False
        if now is not None:
            tpl.seen = now
            if old is not None and old.seen is not None:
                tpl.gap = max(old.gap, now - old.seen)
                tpl.life = min(TEMPLATE_LIFE_MAX, max(TEMPLATE_LIFE, 3 * tpl.gap))
        table[key] = tpl
        return True

    def drop(self, exporter: str, version: int, domain: int, tid: int) -> None:
        self._by_exporter.get(exporter, {}).pop((version, domain, tid), None)

    def drop_domain(self, exporter: str, version: int, domain: int) -> int:
        """All templates and the sampling rate of one observation domain (the exporter restarted); returns how many."""
        table = self._by_exporter.get(exporter, {})
        gone = [k for k in table if k[0] == version and k[1] == domain]
        for k in gone:
            del table[k]
        self.sampling.pop((exporter, version, domain), None)
        return len(gone)

    def sweep(self, now, exporter: Optional[str] = None) -> int:
        """Remove expired templates and forgotten sampling rates (of one exporter or all); returns the number of templates."""
        if now is None:
            return 0
        n = 0
        for ex in ([exporter] if exporter is not None else list(self._by_exporter)):
            table = self._by_exporter.get(ex, {})
            gone = [k for k, t in table.items() if t.expired(now)]
            for k in gone:
                del table[k]
            if gone:
                self.expired[ex] = self.expired.get(ex, 0) + len(gone)
                n += len(gone)
        self.sampling = {k: v for k, v in self.sampling.items() if now - v[1] <= SAMPLING_LIFE}
        return n

    def rate(self, skey, now=None):
        hit = self.sampling.get(skey)
        if hit is None or (now is not None and now - hit[1] > SAMPLING_LIFE):
            return None
        return hit[0]

    def set_rate(self, skey, rate, now=None) -> None:
        self.sampling[skey] = (rate, now if now is not None else 0.0)

    def count(self, exporter: str) -> int:
        return len(self._by_exporter.get(exporter, {}))

    def forget(self, exporter: str) -> None:
        self._by_exporter.pop(exporter, None)
        self.sampling = {k: v for k, v in self.sampling.items() if k[0] != exporter}
        self.uptime = {k: v for k, v in self.uptime.items() if k[0] != exporter}
        self.expired.pop(exporter, None)
        self.restarts.pop(exporter, None)


def new_counters() -> dict:
    return {"malformed": 0, "waiting": 0, "refused": 0}


def _new_exporter() -> dict:
    """Figures of one allowed exporter (shown per exporter in the settings)."""
    return {"packets": 0, "flows": 0, "booked": 0, "bytes": 0, "first": None, "last": None, "version": None, **new_counters()}


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
               cache: Optional[TemplateCache] = None, skey=None, now=None) -> None:
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
            if rate and cache is not None:
                cache.set_rate(skey, rate, now)
            continue
        nbytes = b_in if b_in is not None else b_out
        if rate is None and cache is not None:
            rate = cache.rate(skey, now)
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


def _parse_template_set(buf, off, end, exporter, ver, domain, cache: TemplateCache, counters, ipfix: bool, now=None) -> None:
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
        if tpl.minlen <= 0 or not cache.put(exporter, ver, domain, tid, tpl, now):
            counters["refused"] += 1


def _parse_options_set(buf, off, end, exporter, ver, domain, cache: TemplateCache, counters, ipfix: bool, now=None) -> None:
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
        tpl = Template(fields, options=True)        # without fields: only a mark, so its data sets are not 'waiting'
        if tid >= 256 and not cache.put(exporter, ver, domain, tid, tpl, now):
            counters["refused"] += 1


def _parse_sets(buf, off, end, exporter, ver, domain, cache, counters, ipfix: bool, now=None) -> list:
    flows: list = []
    tpl_id, opt_id = (2, 3) if ipfix else (0, 1)
    while end - off >= 4:
        sid, length = struct.unpack_from(">HH", buf, off)
        if length < 4 or off + length > end:
            counters["malformed"] += 1
            break
        body, stop = off + 4, off + length
        if sid == tpl_id:
            _parse_template_set(buf, body, stop, exporter, ver, domain, cache, counters, ipfix, now)
        elif sid == opt_id:
            _parse_options_set(buf, body, stop, exporter, ver, domain, cache, counters, ipfix, now)
        elif sid >= 256:
            tpl = cache.get(exporter, ver, domain, sid, now)
            if tpl is None:
                counters["waiting"] += 1
            else:
                _read_data(tpl, buf, body, stop, flows, counters, cache, (exporter, ver, domain), now)
        off = stop
    return flows


def _v9_restarted(cache: TemplateCache, exporter: str, domain: int, uptime: int) -> bool:
    """Remember the v9 uptime of (exporter, domain); True when it went back by more than a minute (not a 49.7-day wrap)."""
    key = (exporter, 9, domain)
    last = cache.uptime.get(key)
    if last is not None and uptime + V9_RESTART_BACK_MS < last:
        if not (last > _V9_WRAP_MS - 3_600_000 and uptime < 3_600_000):
            cache.uptime[key] = uptime
            return True
    elif last is not None and uptime < last:
        return False                                # a late packet: keep the newer uptime
    cache.uptime[key] = uptime
    return False


def parse_v9(data: bytes, exporter: str, cache: TemplateCache, counters: dict, now=None) -> list:
    """NetFlow v9 (RFC 3954) -> [(src, dst, bytes)]; learns templates into `cache`."""
    if len(data) < 20:
        counters["malformed"] += 1
        return []
    uptime, domain = struct.unpack_from(">I", data, 4)[0], struct.unpack_from(">I", data, 16)[0]
    if _v9_restarted(cache, exporter, domain, uptime):
        cache.drop_domain(exporter, 9, domain)      # before this packet's sets: it usually brings the new templates
        cache.restarts[exporter] = cache.restarts.get(exporter, 0) + 1
    return _parse_sets(data, 20, len(data), exporter, 9, domain, cache, counters, False, now)


def parse_ipfix(data: bytes, exporter: str, cache: TemplateCache, counters: dict, now=None) -> list:
    """IPFIX (RFC 7011) -> [(src, dst, bytes)]; learns templates into `cache`."""
    if len(data) < 16:
        counters["malformed"] += 1
        return []
    length, domain = struct.unpack_from(">H", data, 2)[0], struct.unpack_from(">I", data, 12)[0]
    if length < 16 or length > len(data):
        counters["malformed"] += 1
        return []
    return _parse_sets(data, 16, length, exporter, 10, domain, cache, counters, True, now)


def parse_packet(data: bytes, exporter: str, cache: TemplateCache, counters: dict, now=None):
    """Any supported datagram -> (version or None, [(src, dst, bytes)]). Never raises."""
    try:
        if len(data) < 2:
            counters["malformed"] += 1
            return None, []
        ver = (data[0] << 8) | data[1]
        if ver == 5:
            return 5, parse_v5(data, counters)
        if ver == 9:
            return 9, parse_v9(data, exporter, cache, counters, now)
        if ver == 10:
            return 10, parse_ipfix(data, exporter, cache, counters, now)
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
                   "skipped_self": 0, "lan_pairs": 0, **new_counters()}
        self._last_packet = self._last_flow = None
        self._drop_seen: set = set()
        self._drop_pending = 0
        self._drop_logged = None
        self._refused: dict = {}            # sender -> {"packets", "last", "version"}: flow packets from senders not allowed
        self._swept = None

    # -- configuration
    def configure(self, exporters: list, port: int) -> None:
        allowed = {t for t in (_norm_ip(e) for e in (exporters or [])) if t}
        with self._lock:
            for gone in self._allowed - allowed:
                self._cache.forget(gone)
                self._ex.pop(gone, None)
            for ip in allowed:
                self._ex.setdefault(ip, _new_exporter())
                self._refused.pop(ip, None)         # allowed now: no longer offered
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
                if self._swept is None or not 0 <= now - self._swept < SWEEP_EVERY:
                    self._swept = now
                    self._cache.sweep(now)
                counters = new_counters()
                ver, flows = parse_packet(bytes(data), exporter, self._cache, counters, now)
                ex = self._ex[exporter]
                for k, v in counters.items():
                    self._c[k] += v
                    ex[k] += v
                ex["packets"] += 1
                ex["flows"] += len(flows)
                ex["last"] = now
                if ex["first"] is None:
                    ex["first"] = now
                if ver:
                    ex["version"] = ver
                self._c["flows"] += len(flows)
                if flows:
                    self._last_flow = now
            else:
                self._c["dropped_foreign"] += 1
        if not known:
            self._note_drop(exporter or str(sender_ip)[:64], now, data, exporter is not None)
            return 0
        return self._book(flows, now, exporter) if flows else 0

    def _note_drop(self, sender: str, now: float, data: bytes = b"", is_ip: bool = False) -> None:
        ver = ((data[0] << 8) | data[1]) if len(data) >= 2 else None
        with self._lock:
            if is_ip and ver in (5, 9, 10):         # looks like flow export: offered in the settings ("Allow")
                hit = self._refused.get(sender)
                if hit is None:
                    if len(self._refused) >= MAX_REFUSED_SHOWN:
                        del self._refused[min(self._refused, key=lambda k: self._refused[k]["last"])]
                    hit = self._refused[sender] = {"packets": 0, "last": now, "version": ver}
                hit["packets"] += 1
                hit["last"], hit["version"] = now, ver
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
    def _book(self, flows: list, now: float, exporter: Optional[str] = None) -> int:
        try:
            nets = [n if not isinstance(n, str) else ipaddress.ip_network(n, strict=False) for n in self._lan_networks()]
        except Exception:
            nets = []
        try:
            skip = set(self._skip_ips() or ())
        except Exception:
            skip = set()
        memo: dict = {}
        booked = nbytes = local = own = pairs = 0
        for src, dst, size in flows:
            if str(src) in skip or str(dst) in skip:
                own += 1
                continue
            ks, kd = self._kind(src, nets, memo), self._kind(dst, nets, memo)
            size = min(size, MAX_FLOW_BYTES)
            if size <= 0 or ks[0] == kd[0] or ks[0] is None or kd[0] is None:
                if size > 0 and ks[0] == "lan" and kd[0] == "lan":
                    # LAN <-> LAN: not a data path, but a conversation between two devices (the map's "Who talks to whom");
                    # only when both are known devices, so an unknown address never becomes a line on the map
                    if ks[1] != UNKNOWN_DEVICE and kd[1] != UNKNOWN_DEVICE and ks[1] != kd[1]:
                        try:
                            if self._paths.book_pair(ks[1], kd[1], min(size, MAX_FLOW_BYTES), now):
                                pairs += 1
                        except Exception:
                            log.debug("netflow pair error", exc_info=True)
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
            self._c["lan_pairs"] += pairs
            ex = self._ex.get(exporter)
            if ex is not None:
                ex["booked"] += booked
                ex["bytes"] += nbytes
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
        now = self._clock()
        with self._lock:
            c = self._c
            exporters = {}
            for ip, v in self._ex.items():
                exporters[ip] = dict(v, templates=self._cache.count(ip), expired=self._cache.expired.get(ip, 0),
                                     restarts=self._cache.restarts.get(ip, 0))
            refused = sorted(({"ip": ip, **v} for ip, v in self._refused.items() if now - v["last"] <= REFUSED_SHOWN_FOR),
                             key=lambda r: -r["last"])
            return {
                "packets": c["packets"], "flows": c["flows"], "booked": c["booked"], "bytes": c["bytes"],
                "dropped_foreign": c["dropped_foreign"], "malformed": c["malformed"], "waiting": c["waiting"],
                "refused": c["refused"], "skipped_local": c["skipped_local"], "skipped_self": c["skipped_self"],
                "lan_pairs": c["lan_pairs"],
                "last_packet": self._last_packet, "last_flow": self._last_flow,
                "exporters": exporters, "refused_senders": refused,
                "listening": self._transport is not None, "port": self._port, "error": self._error,
            }
