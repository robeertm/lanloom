"""TCP conversations with bytes and listening services of the agent's host.

Bytes per TCP connection come from the kernel's socket diagnostics over netlink (the same reader as Lanloom's own
host source, ``lanloom.sources.hostnet``); with host networking the agent sees every socket of the host without any
privilege. Between two reports the counters are summed per conversation: local address, peer address and the service
port (the peer's port for an outgoing connection, the own port for an incoming one) - single connections and
ephemeral ports are not kept.

Listening services come from the kernel's TCP and UDP tables (``/proc/net/tcp*``/``udp*``): address, port and - when
the agent may see it - the short process name (``/proc/<pid>/comm``, at most 15 characters, never the command line).
"""
from __future__ import annotations

import os
import socket

from ..sources import hostnet
from . import scrub

MAX_FLOWS = 600
MAX_LISTEN = 300


def _is_loop(ip: str) -> bool:
    return hostnet.is_loopback(ip) or ip in ("", "0.0.0.0", "::")


class FlowMeter:
    """Kernel counters -> summed bytes per conversation since the last ``take()``."""

    def __init__(self):
        self.reader = None
        self.counter = hostnet.ByteCounter()
        self.acc: dict = {}
        self.listen_ports: set = set()
        self.available = None

    def _open(self) -> bool:
        if self.reader is not None:
            return True
        if not hostnet.supported():
            self.available = False
            return False
        try:
            self.reader = hostnet.NetlinkReader(destroy=True)
            self.available = True
        except OSError:
            self.available = False
            return False
        return True

    def poll(self) -> None:
        """One reading of the counters (call every few seconds; closed sockets are caught by destroy notifications)."""
        if not self._open():
            return
        try:
            snap = self.reader.dump()
            destroyed = self.reader.drain_destroyed()
        except OSError:
            self.reader.close()
            self.reader = None
            self.counter.reset()
            return
        deltas, closed = self.counter.update(snap, destroyed)
        by_key = {c["key"]: c for c in snap}
        for k, (dt, dr) in deltas.items():
            if dt or dr:
                self._add(by_key[k], dt, dr)
        for c, dt, dr in closed:
            self._add(c, dt, dr)

    def _add(self, c: dict, tx: int, rx: int) -> None:
        lip, rip = c["lip"], c["rip"]
        if _is_loop(lip) or _is_loop(rip):
            return
        incoming = c["lport"] in self.listen_ports
        key = (lip, rip, c["lport"] if incoming else c["rport"], "in" if incoming else "out")
        acc = self.acc.get(key)
        if acc is None:
            if len(self.acc) >= MAX_FLOWS * 4:
                return
            acc = self.acc[key] = [0, 0]
        acc[0] += tx
        acc[1] += rx

    def take(self) -> list:
        """Summed conversations since the last call: [{lip, rip, port, dir, tx, rx}], biggest first, capped."""
        acc, self.acc = self.acc, {}
        rows = [{"lip": k[0], "rip": k[1], "port": k[2], "dir": k[3], "tx": v[0], "rx": v[1]} for k, v in acc.items()]
        rows.sort(key=lambda r: -(r["tx"] + r["rx"]))
        return rows[:MAX_FLOWS]

    def close(self) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None


# ------------------------------------------------------------------ listening services
def _parse_table(text: str, v6: bool, proto: str) -> list:
    out = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 10:
            continue
        state = f[3]
        if proto == "tcp" and state != "0A":          # LISTEN
            continue
        if proto == "udp" and state != "07":          # unconnected UDP socket
            continue
        try:
            ip_hex, port_hex = f[1].split(":")
            ip = hostnet._ip6_hex(ip_hex) if v6 else hostnet._ip4_hex(ip_hex)
            port = int(port_hex, 16)
            inode = int(f[9])
        except (ValueError, IndexError):
            continue
        if not port:
            continue
        out.append({"proto": proto, "ip": ip, "port": port, "inode": inode})
    return out


def _inode_names(proc: str, wanted: set) -> dict:
    """socket inode -> short process name, for the processes the agent may look at (best effort)."""
    names = {}
    if not wanted:
        return names
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return names
    for pid in pids[:4000]:
        fd_dir = "%s/%s/fd" % (proc, pid)
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        comm = None
        for fd in fds[:2000]:
            try:
                link = os.readlink(fd_dir + "/" + fd)
            except OSError:
                continue
            if link.startswith("socket:["):
                try:
                    ino = int(link[8:-1])
                except ValueError:
                    continue
                if ino in wanted and ino not in names:
                    if comm is None:
                        try:
                            with open("%s/%s/comm" % (proc, pid), "r", encoding="utf-8", errors="replace") as fh:
                                comm = fh.read(32).strip()
                        except OSError:
                            comm = ""
                    names[ino] = comm
        if len(names) >= len(wanted):
            break
    return names


def listening(proc: str = "/proc", names: bool = True) -> list:
    rows = []
    for fname, v6, proto in (("tcp", False, "tcp"), ("tcp6", True, "tcp"), ("udp", False, "udp"), ("udp6", True, "udp")):
        try:
            with open("%s/net/%s" % (proc, fname), "r", encoding="ascii", errors="replace") as fh:
                rows.extend(_parse_table(fh.read(1 << 22), v6, proto))
        except OSError:
            continue
    procs = _inode_names(proc, {r["inode"] for r in rows if r["inode"]}) if names else {}
    lo, hi = ephemeral(proc)
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r["proto"], r["port"], r["ip"])):
        if r["proto"] == "udp" and lo <= r["port"] <= hi and r["ip"] not in ("0.0.0.0", "::"):
            continue                                   # client sockets in the ephemeral range, not services
        key = (r["proto"], r["port"], r["ip"])
        if key in seen:
            continue
        seen.add(key)
        item = {"proto": r["proto"], "ip": r["ip"], "port": r["port"], "local": hostnet.is_loopback(r["ip"])}
        name = procs.get(r["inode"])
        if name:
            item["proc"] = scrub.text(name, 15)
        out.append(item)
    return out[:MAX_LISTEN]


def ephemeral(proc: str = "/proc") -> tuple:
    try:
        with open(proc + "/sys/net/ipv4/ip_local_port_range", "r", encoding="ascii") as fh:
            a, b = fh.read(64).split()[:2]
        return int(a), int(b)
    except (OSError, ValueError):
        return 32768, 60999


def ports_of(listen: list) -> set:
    return {r["port"] for r in listen if r["proto"] == "tcp"}


def family_ok() -> bool:
    return hasattr(socket, "AF_NETLINK")
