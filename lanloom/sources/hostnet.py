"""Host network source: measured bytes per TCP flow and per interface, once per second. No credentials.

TCP bytes come from the kernel's socket diagnostics over netlink (sock_diag / inet_diag with the ``tcp_info``
extension: ``bytes_acked`` = sent and acknowledged, ``bytes_received``). Reading the sockets of the own network
namespace needs no privileges, so inside a container with host networking this sees all TCP flows of the host.

Byte conservation: every socket carries its kernel counter; a frame reports only the increase since the previous
frame. A socket that appeared between two frames counts from zero (its whole traffic is new); on the very first
frame only the baseline is taken (old traffic is not new). A socket that closed since the last frame has already
been accounted up to its last-known value and adds nothing more - unless the kernel's destroy notifications are
available (needs the network-admin capability), which carry the final counters; then exactly the remainder
(final - last known) is added, never twice.

Interface counters come from the kernel's per-interface statistics file; loopback, virtual ethernet pairs and
bridges are left out (they would count container traffic twice).

Fallback without netlink: flows from the kernel's TCP table without bytes (``"tx": null``). On a platform without
netlink socket diagnostics at all: status ``source.hostnet.unsupported`` and ``run()`` returns.
"""
from __future__ import annotations

import asyncio
import os
import socket
import struct
import sys
import time

from ._common import Backoff, StatusTracker, emit_event, now

MODULE = "hostnet"
SOURCE = "host"

MAX_FLOWS = 2000

# Kernel TCP state numbers (same in the netlink reply and in the TCP table, hex there)
TCP_STATES = {1: "ESTABLISHED", 2: "SYN_SENT", 3: "SYN_RECV", 4: "FIN_WAIT1", 5: "FIN_WAIT2", 6: "TIME_WAIT",
              7: "CLOSE", 8: "CLOSE_WAIT", 9: "LAST_ACK", 10: "LISTEN", 11: "CLOSING"}
BYTE_STATES = (1, 4, 5, 8, 9, 11)        # states with a full socket (tcp_info available)
BYTE_STATE_MASK = sum(1 << s for s in BYTE_STATES)

_NLMSG = struct.Struct("=IHHII")          # nlmsghdr: len, type, flags, seq, pid
_REQ = struct.Struct("=BBBBI")            # inet_diag_req_v2: family, protocol, ext, pad, states (+ 48 byte id)
_ATTR = struct.Struct("=HH")              # rtattr: len, type
SOCK_DIAG_BY_FAMILY = 20
NLMSG_DONE = 3
NLMSG_ERROR = 2
NLM_F_REQUEST = 0x1
NLM_F_DUMP = 0x300
INET_DIAG_INFO = 2
NETLINK_SOCK_DIAG = 4
IPPROTO_TCP = 6
DIAG_MSG_LEN = 72                         # sizeof(struct inet_diag_msg)
OFF_BYTES_ACKED = 120                     # struct tcp_info: tcpi_bytes_acked (u64)
OFF_BYTES_RECEIVED = 128                  # struct tcp_info: tcpi_bytes_received (u64)
DESTROY_GROUPS = (1 << (1 - 1)) | (1 << (3 - 1))    # SKNLGRP_INET_TCP_DESTROY, SKNLGRP_INET6_TCP_DESTROY
ENOBUFS = 105


def supported() -> bool:
    return sys.platform.startswith("linux") and hasattr(socket, "AF_NETLINK")  # identifier: platform name


# ================================================================== netlink codec
def diag_request(family: int, seq: int = 1, states: int = BYTE_STATE_MASK) -> bytes:
    body = _REQ.pack(family, IPPROTO_TCP, 1 << (INET_DIAG_INFO - 1), 0, states) + b"\0" * 48
    return _NLMSG.pack(_NLMSG.size + len(body), SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST | NLM_F_DUMP, seq, 0) + body


def _address(family: int, raw: bytes) -> str:
    if family == socket.AF_INET:
        return socket.inet_ntop(socket.AF_INET, raw[:4])
    ip = socket.inet_ntop(socket.AF_INET6, raw[:16])
    return ip[7:] if ip.startswith("::ffff:") and "." in ip else ip


def parse_diag(buf: bytes, include_unmetered: bool = False) -> tuple[list[dict], bool]:
    """One netlink read -> ([conn], done). conn: lip, lport, rip, rport, inode, key, state, acked, recv.
    Without ``include_unmetered`` only sockets carrying tcp_info counters are returned."""
    out, i, done = [], 0, False
    while i + _NLMSG.size <= len(buf):
        length, mtype, _, _, _ = _NLMSG.unpack_from(buf, i)
        if length < _NLMSG.size or i + length > len(buf):
            break
        if mtype == NLMSG_DONE:
            done = True
        elif mtype == NLMSG_ERROR:
            err = struct.unpack_from("=i", buf, i + _NLMSG.size)[0] if length >= _NLMSG.size + 4 else -1
            if err:
                raise OSError(-err, "inet_diag")
            done = True
        elif mtype == SOCK_DIAG_BY_FAMILY and length >= _NLMSG.size + DIAG_MSG_LEN:
            m = i + _NLMSG.size
            family, state = buf[m], buf[m + 1]
            if family in (socket.AF_INET, socket.AF_INET6):
                lport, rport = struct.unpack_from(">HH", buf, m + 4)
                src, dst = buf[m + 8:m + 24], buf[m + 24:m + 40]
                cookie = struct.unpack_from("=Q", buf, m + 44)[0]
                inode = struct.unpack_from("=I", buf, m + 68)[0]
                acked = recv = None
                a, end = m + DIAG_MSG_LEN, i + length
                while a + _ATTR.size <= end:
                    alen, atype = _ATTR.unpack_from(buf, a)
                    if alen < _ATTR.size or a + alen > end:
                        break
                    if atype == INET_DIAG_INFO and alen - _ATTR.size >= OFF_BYTES_RECEIVED + 8:
                        acked, recv = struct.unpack_from("=QQ", buf, a + _ATTR.size + OFF_BYTES_ACKED)
                    a += (alen + 3) & ~3
                if acked is not None or include_unmetered:
                    out.append({"lip": _address(family, src), "lport": lport, "rip": _address(family, dst),
                                "rport": rport, "inode": inode, "key": cookie,
                                "state": TCP_STATES.get(state, str(state)), "acked": acked, "recv": recv})
        i += (length + 3) & ~3
    return out, done


class NetlinkReader:
    """Dump of the own namespace's TCP sockets with byte counters; optional destroy notifications."""

    def __init__(self, destroy: bool = True):
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG)
        self.sock.settimeout(2.0)
        self.seq = 0
        self.destroy_sock = None
        self.destroy_lost = 0
        if destroy:
            try:
                d = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_SOCK_DIAG)
                try:
                    d.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                    d.bind((0, DESTROY_GROUPS))
                except OSError:
                    d.close()
                    raise
                d.setblocking(False)
                self.destroy_sock = d
            except OSError:          # no network-admin capability: snapshots only
                self.destroy_sock = None

    def dump(self) -> list[dict]:
        out = []
        for fam in (socket.AF_INET, socket.AF_INET6):
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            self.sock.send(diag_request(fam, self.seq))
            for _ in range(100000):
                part, done = parse_diag(self.sock.recv(262144))
                out.extend(part)
                if done:
                    break
        return out

    def drain_destroyed(self, limit: int = 4096) -> list[dict]:
        if self.destroy_sock is None:
            return []
        out = []
        for _ in range(limit):
            try:
                buf = self.destroy_sock.recv(65536)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == ENOBUFS:      # kernel buffer overflowed: notifications lost, keep reading
                    self.destroy_lost += 1
                    continue
                break
            if not buf:
                break
            try:
                part, _ = parse_diag(buf)
            except OSError:
                continue
            out.extend(part)
        return out

    def close(self) -> None:
        for s in (self.sock, self.destroy_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


# ================================================================== fallback and interfaces
def _ip4_hex(h: str) -> str:
    return socket.inet_ntop(socket.AF_INET, struct.pack("=I", int(h, 16)))


def _ip6_hex(h: str) -> str:
    raw = b"".join(struct.pack("=I", int(h[i:i + 8], 16)) for i in range(0, 32, 8))
    ip = socket.inet_ntop(socket.AF_INET6, raw)
    return ip[7:] if ip.startswith("::ffff:") and "." in ip else ip


def parse_tcp_table(text: str, v6: bool) -> list[dict]:
    """Kernel TCP table (text) -> connections in byte-capable states, without counters."""
    out = []
    for line in text.splitlines()[1:]:
        t = line.split()
        if len(t) < 10:
            continue
        try:
            lh, lp = t[1].split(":")
            rh, rp = t[2].split(":")
            state = int(t[3], 16)
            conv = _ip6_hex if v6 else _ip4_hex
            c = {"lip": conv(lh), "lport": int(lp, 16), "rip": conv(rh), "rport": int(rp, 16),
                 "inode": int(t[9]) if t[9].isdigit() else 0, "key": None,
                 "state": TCP_STATES.get(state, str(state)), "acked": None, "recv": None}
        except (ValueError, OSError):
            continue
        if state in BYTE_STATES:
            out.append(c)
    return out


def read_tcp_tables(proc: str = "/proc") -> list[dict] | None:
    out, any_ok = [], False
    for name, v6 in (("tcp", False), ("tcp6", True)):
        try:
            with open(os.path.join(proc, "net", name)) as f:
                out.extend(parse_tcp_table(f.read(), v6))
            any_ok = True
        except OSError:
            continue
    return out if any_ok else None


def parse_net_dev(text: str) -> dict[str, tuple[int, int]]:
    """Per-interface statistics text -> {name: (rx_bytes, tx_bytes)}."""
    out = {}
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        t = rest.split()
        if name and len(t) >= 9:
            try:
                out[name] = (int(t[0]), int(t[8]))
            except ValueError:
                continue
    return out


def skip_interface(name: str, sys_net: str = "/sys/class/net") -> bool:
    """Loopback, virtual ethernet pairs and bridges would count container traffic twice."""
    if name == "lo" or name.startswith("veth"):
        return True
    return os.path.isdir(os.path.join(sys_net, name, "bridge"))


def counter_delta(old: int, new: int) -> int | None:
    if new >= old:
        return new - old
    if old < (1 << 32) and new < (1 << 32):    # 32-bit kernel counter wrapped
        return new + (1 << 32) - old
    return None                                 # reset


class IfaceCounter:
    def __init__(self, proc: str = "/proc", sys_net: str = "/sys/class/net"):
        self.path = os.path.join(proc, "net", "dev")
        self.sys_net = sys_net
        self.last: dict[str, tuple[int, int]] | None = None

    def read(self) -> dict[str, tuple[int, int]] | None:
        try:
            with open(self.path) as f:
                stats = parse_net_dev(f.read())
        except OSError:
            return None
        # interfaces that never carried a byte (idle tunnel/bond templates) are noise on the map
        return {n: v for n, v in stats.items() if (v[0] or v[1]) and not skip_interface(n, self.sys_net)}

    def deltas(self, stats: dict[str, tuple[int, int]]) -> list[dict] | None:
        prev, self.last = self.last, stats
        if prev is None:
            return None
        out = []
        for name, (rx, tx) in sorted(stats.items()):
            if name not in prev:
                continue
            drx, dtx = counter_delta(prev[name][0], rx), counter_delta(prev[name][1], tx)
            if drx is None or dtx is None:
                continue
            out.append({"name": name, "tx": dtx, "rx": drx})
        return out


# ================================================================== byte accounting
class ByteCounter:
    """Kernel counters per socket -> deltas. See the module docstring for the conservation rules."""

    def __init__(self):
        self.last: dict[int, tuple[int, int]] = {}
        self.started = False
        self.closed_bytes = 0          # bytes that only arrived through destroy notifications (diagnostics)

    def reset(self) -> None:
        self.last = {}
        self.started = False

    def update(self, snapshot: list[dict], destroyed: list[dict] = ()) -> tuple[dict, list]:
        """-> ({key: (d_tx, d_rx)} for sockets in the snapshot, [(conn, d_tx, d_rx)] for destroyed sockets)."""
        first = not self.started
        current, deltas = {}, {}
        for c in snapshot:
            k = c["key"]
            current[k] = (c["acked"], c["recv"])
            prev = self.last.get(k)
            if prev is None:
                if first:
                    continue
                prev = (1 if c["acked"] else 0, 0)     # bytes_acked counts the SYN as one
            da, dr = c["acked"] - prev[0], c["recv"] - prev[1]
            if da < 0 or dr < 0:
                continue
            deltas[k] = (da, dr)
        closed = []
        if not first:
            for c in destroyed:
                k = c["key"]
                base = current.pop(k, None) or self.last.get(k)
                if base is None:                       # opened and closed between two frames
                    base = (1 if c["acked"] else 0, 0)
                da, dr = c["acked"] - base[0], c["recv"] - base[1]
                if da < 0 or dr < 0 or not (da or dr):
                    continue
                closed.append((c, da, dr))
                self.closed_bytes += da + dr
        self.last = current
        self.started = True
        return deltas, closed


def is_loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip == "::1"


def _unspecified(ip: str) -> bool:
    return ip in ("0.0.0.0", "::")


def build_flows(snapshot: list[dict], deltas: dict | None, closed: list = (), include_loopback: bool = False,
                cap: int = MAX_FLOWS) -> list[dict]:
    """Sockets + deltas -> flow list (merged per 4-tuple, capped). ``deltas=None``: no byte counters (fallback)."""
    merged: dict[tuple, dict] = {}

    def add(c, tx, rx):
        if _unspecified(c["rip"]) or (not include_loopback and (is_loopback(c["rip"]) or is_loopback(c["lip"]))):
            return
        k = (c["lip"], c["lport"], c["rip"], c["rport"])
        f = merged.get(k)
        if f is None:
            merged[k] = {"local_ip": c["lip"], "local_port": c["lport"], "peer_ip": c["rip"], "peer_port": c["rport"],
                         "tx": tx, "rx": rx}
        elif tx is not None:
            f["tx"] = (f["tx"] or 0) + tx
            f["rx"] = (f["rx"] or 0) + rx

    for c in snapshot:
        if deltas is None:
            add(c, None, None)
        else:
            d = deltas.get(c["key"], (0, 0))
            add(c, d[0], d[1])
    for c, da, dr in closed:
        add(c, da, dr)
    return cap_flows(list(merged.values()), cap)


def cap_flows(flows: list[dict], cap: int = MAX_FLOWS) -> list[dict]:
    """At most ``cap`` entries: the busiest flows individually, the rest aggregated per peer_ip."""
    if len(flows) <= cap:
        return flows
    flows = sorted(flows, key=lambda f: -((f["tx"] or 0) + (f["rx"] or 0)))
    keep = cap
    while keep > 0:
        peers = len({f["peer_ip"] for f in flows[keep:]})
        if keep + peers <= cap:
            break
        keep = max(0, cap - peers)
    rest: dict[str, dict] = {}
    for f in flows[keep:]:
        a = rest.get(f["peer_ip"])
        if a is None:
            a = rest[f["peer_ip"]] = {"local_ip": None, "local_port": None, "peer_ip": f["peer_ip"], "peer_port": None,
                                      "tx": f["tx"], "rx": f["rx"], "aggregated": 0}
        elif f["tx"] is not None:
            a["tx"] = (a["tx"] or 0) + f["tx"]
            a["rx"] = (a["rx"] or 0) + f["rx"]
        a["aggregated"] += 1
    aggregates = sorted(rest.values(), key=lambda f: -((f["tx"] or 0) + (f["rx"] or 0)))
    room = cap - keep
    if len(aggregates) > room:                     # more distinct peers than room: fold the tail into one entry
        tail = aggregates[room - 1:]
        other = {"local_ip": None, "local_port": None, "peer_ip": None, "peer_port": None,
                 "tx": None if tail[0]["tx"] is None else sum(a["tx"] or 0 for a in tail),
                 "rx": None if tail[0]["rx"] is None else sum(a["rx"] or 0 for a in tail),
                 "aggregated": sum(a["aggregated"] for a in tail)}
        aggregates = aggregates[:room - 1] + [other]
    return flows[:keep] + aggregates


# ================================================================== source
class HostNet:
    """Live source: TCP flow bytes and interface counters of the host (needs host networking in a container)."""

    name = SOURCE

    def __init__(self, interval=1.0, proc="/proc", sys_net="/sys/class/net", include_loopback=False,
                 max_flows=MAX_FLOWS, destroy_events=True):
        self.interval = max(0.2, float(interval))
        self.proc = str(proc)
        self.include_loopback = bool(include_loopback)
        self.max_flows = max(10, int(max_flows))
        self.destroy_events = bool(destroy_events)
        self.counter = ByteCounter()
        self.ifaces = IfaceCounter(self.proc, sys_net)
        self._reader: NetlinkReader | None = None
        self._retry_at = 0.0
        self._backoff = Backoff()
        self.mode = None

    def __repr__(self):
        return "HostNet(mode=%r)" % self.mode

    def _open_reader(self) -> None:
        if self._reader is None and time.monotonic() >= self._retry_at:
            try:
                self._reader = NetlinkReader(self.destroy_events)
                self.counter.reset()
            except OSError:
                self._retry_at = time.monotonic() + self._backoff.next()

    def tick(self) -> tuple[list[dict], dict | None]:
        """One blocking measurement -> (frames, status) where status = (ok, key)."""
        t = now()
        frames = []
        self._open_reader()
        flows = None
        if self._reader is not None:
            try:
                snap = self._reader.dump()
                destroyed = self._reader.drain_destroyed()
                deltas, closed = self.counter.update(snap, destroyed)
                flows = build_flows(snap, deltas, closed, self.include_loopback, self.max_flows)
                self.mode = "netlink"
                self._backoff.reset()
            except OSError:
                self._reader.close()
                self._reader = None
                self._retry_at = time.monotonic() + self._backoff.next()
        if flows is None:
            table = read_tcp_tables(self.proc)
            if table is not None:
                flows = build_flows(table, None, (), self.include_loopback, self.max_flows)
                self.mode = "table"
        if flows is not None:
            frames.append({"type": "bytes", "source": SOURCE, "t": t, "flows": flows})
        stats = self.ifaces.read()
        if stats is not None:
            d = self.ifaces.deltas(stats)
            if d is not None:
                frames.append({"type": "iface", "source": SOURCE, "t": t, "ifaces": d})
        if flows is None and stats is None:
            self.mode = None
            return frames, (False, "source.hostnet.unavailable")
        if self.mode == "netlink":
            return frames, (True, "source.hostnet.ok")
        return frames, (True, "source.hostnet.no_bytes")

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    async def run(self, emit) -> None:
        status = StatusTracker(SOURCE)
        if not supported():
            await status.set(emit, False, "source.hostnet.unsupported")
            return
        next_tick = time.monotonic()
        try:
            while True:
                try:
                    frames, st = await asyncio.to_thread(self.tick)
                except asyncio.CancelledError:
                    raise
                except Exception:          # never die: the gap shows up as missing frames
                    frames, st = [], (False, "source.hostnet.error")
                for f in frames:
                    await emit_event(emit, f)
                await status.set(emit, *st)
                next_tick += self.interval
                if next_tick < time.monotonic() - 2 * self.interval:
                    next_tick = time.monotonic() + self.interval
                await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
        finally:
            self.close()

    async def test(self):
        if not supported():
            return False, "source.hostnet.unsupported", {}
        try:
            frames, (ok, key) = await asyncio.to_thread(self.tick)
        except Exception:
            return False, "source.hostnet.error", {}
        flows = next((f["flows"] for f in frames if f["type"] == "bytes"), [])
        stats = self.ifaces.last or {}
        return ok, key, {"mode": self.mode, "flows": len(flows), "ifaces": len(stats)}
