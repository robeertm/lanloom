"""Failed services and timer results of the host's service manager, read over the system message bus.

A minimal, read-only client of the message bus wire protocol (no package, no program): it authenticates with the
agent's user id, lists the units and reads a few properties of the timers and failed units. It can call exactly the
methods in ``ALLOWED`` - listing and reading properties - and nothing that starts, stops or changes a unit; a test
checks that no other method name appears in this file.

Kept per failed unit: name, result and since when. Per timer: name, the unit it starts, last and next run and the
result of the last run. Unit descriptions, command lines and environment are never read.
"""
from __future__ import annotations

import os
import socket
import struct

BUS = "/run/dbus/system_bus_socket"
SVC = "org.freedesktop.systemd1"  # identifier: bus name of the service manager
OBJ = "/org/freedesktop/systemd1"  # identifier: object path of the service manager
# (interface, method) pairs this client can send - read-only by construction
ALLOWED = frozenset({("org.freedesktop.DBus", "Hello"),  # identifier: message bus interface
                     ("org.freedesktop.systemd1.Manager", "ListUnits"),  # identifier: service manager interface
                     ("org.freedesktop.DBus.Properties", "Get")})  # identifier: properties interface
MAX_TIMERS = 120
MAX_FAILED = 100
TIMEOUT = 3.0

_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8, "h": 4, "s": 4, "o": 4, "g": 1,
          "a": 4, "(": 8, "{": 8, "v": 1}
_FMT = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d", "h": "I"}


class BusError(Exception):
    pass


# ------------------------------------------------------------------ marshalling
def _pad(buf: bytearray, n: int) -> None:
    while len(buf) % n:
        buf.append(0)


def _put(buf: bytearray, sig: str, value) -> None:
    c = sig[0]
    _pad(buf, _ALIGN[c])
    if c in ("s", "o"):
        raw = value.encode("utf-8")
        buf += struct.pack("<I", len(raw)) + raw + b"\0"
    elif c == "g":
        raw = value.encode("ascii")
        buf += struct.pack("<B", len(raw)) + raw + b"\0"
    elif c in _FMT:
        buf += struct.pack("<" + _FMT[c], value)
    elif c == "v":
        vsig, inner = value
        _put(buf, "g", vsig)
        _put(buf, vsig, inner)
    elif c == "(":
        _pad(buf, 8)
        for s, v in zip(_split(sig[1:-1]), value):
            _put(buf, s, v)
    elif c == "a":
        esig = sig[1:]
        start_len = len(buf)
        buf += b"\0\0\0\0"
        _pad(buf, _ALIGN[esig[0]])
        body_start = len(buf)
        for v in value:
            _put(buf, esig, v)
        struct.pack_into("<I", buf, start_len, len(buf) - body_start)
    else:
        raise BusError("cannot write %s" % c)


def _one(sig: str) -> int:
    """Length of the first complete type in ``sig``."""
    c = sig[0]
    if c == "a":
        return 1 + _one(sig[1:])
    if c in "({":
        close = ")" if c == "(" else "}"
        depth, i = 0, 0
        while i < len(sig):
            if sig[i] in "({":
                depth += 1
            elif sig[i] in ")}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise BusError("bad signature")
    return 1


def _split(sig: str) -> list:
    out, i = [], 0
    while i < len(sig):
        n = _one(sig[i:])
        out.append(sig[i:i + n])
        i += n
    return out


class _Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos

    def align(self, n: int) -> None:
        self.p += (-self.p) % n

    def get(self, sig: str, depth: int = 0):
        if depth > 32:
            raise BusError("too deep")
        c = sig[0]
        self.align(_ALIGN[c])
        if c in ("s", "o"):
            (n,) = struct.unpack_from("<I", self.d, self.p)
            self.p += 4
            v = self.d[self.p:self.p + n].decode("utf-8", "replace")
            self.p += n + 1
            return v
        if c == "g":
            n = self.d[self.p]
            self.p += 1
            v = self.d[self.p:self.p + n].decode("ascii", "replace")
            self.p += n + 1
            return v
        if c in _FMT:
            fmt = "<" + _FMT[c]
            (v,) = struct.unpack_from(fmt, self.d, self.p)
            self.p += struct.calcsize(fmt)
            return bool(v) if c == "b" else v
        if c == "v":
            vsig = self.get("g")
            return self.get(vsig, depth + 1)
        if c in "({":
            self.align(8)
            return [self.get(s, depth + 1) for s in _split(sig[1:-1])]
        if c == "a":
            (n,) = struct.unpack_from("<I", self.d, self.p)
            self.p += 4
            esig = sig[1:]
            self.align(_ALIGN[esig[0]])
            end = self.p + n
            items = []
            while self.p < end and len(items) < 100000:
                items.append(self.get(esig, depth + 1))
            self.p = end
            if esig[0] == "{":
                return {k: v for k, v in items}
            return items
        raise BusError("cannot read %s" % c)


# ------------------------------------------------------------------ connection
class Bus:
    def __init__(self, path: str = BUS, timeout: float = TIMEOUT):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.serial = 0
        self.buf = b""
        try:
            self.sock.connect(path)
            self.sock.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
            line = self._line()
            if not line.startswith(b"OK"):
                raise BusError("auth refused")
            self.sock.sendall(b"BEGIN\r\n")
            self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "Hello")  # identifier: bus names
        except (OSError, BusError):
            self.close()
            raise

    def _line(self) -> bytes:
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BusError("closed")
            self.buf += chunk
            if len(self.buf) > 4096:
                raise BusError("auth line too long")
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _need(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise BusError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def call(self, dest: str, path: str, iface: str, member: str, sig: str = "", args: tuple = ()):
        if (iface, member) not in ALLOWED:
            raise BusError("method not allowed")
        self.serial += 1
        body = bytearray()
        for s, v in zip(_split(sig), args):
            _put(body, s, v)
        fields = [(1, ("o", path)), (2, ("s", iface)), (3, ("s", member)), (6, ("s", dest))]
        if sig:
            fields.append((8, ("g", sig)))
        head = bytearray(b"l\x01\x00\x01") + struct.pack("<II", len(body), self.serial)
        _put(head, "a(yv)", fields)
        _pad(head, 8)
        self.sock.sendall(bytes(head) + bytes(body))
        for _ in range(1000):
            fixed = self._need(16)
            if fixed[0:1] != b"l":
                raise BusError("big-endian replies are not supported")
            mtype = fixed[1]
            blen, _serial, flen = struct.unpack_from("<III", fixed, 4)
            if blen > 64 << 20 or flen > 1 << 20:
                raise BusError("message too large")
            rest = self._need(flen + (-(16 + flen)) % 8)
            hdr = _Reader(fixed + rest, 12).get("a(yv)")
            body_raw = self._need(blen)
            hf = {code: val for code, val in hdr}
            if hf.get(5) != self.serial:
                continue                               # a signal or another reply
            rsig = hf.get(8, "")
            if mtype == 3:
                raise BusError(str(hf.get(4, "error")))
            r = _Reader(body_raw)
            return [r.get(s) for s in _split(rsig)] if rsig else []
        raise BusError("no reply")

    def prop(self, path: str, iface: str, name: str):
        return self.call(SVC, path, "org.freedesktop.DBus.Properties", "Get", "ss", (iface, name))[0]  # identifier: interface

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ what the agent reports
def _usec(v) -> float | None:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return round(v / 1e6, 1) if 0 < v < (1 << 63) else None


def units(path: str = BUS) -> dict:
    """{"available": bool, "failed": [...], "timers": [...], "total": n}. Never raises."""
    path = os.environ.get("LANLOOM_AGENT_DBUS", path)
    if not os.path.exists(path):
        return {"available": False, "reason": "no_bus"}
    try:
        bus = Bus(path)
    except (OSError, BusError):
        return {"available": False, "reason": "refused"}
    try:
        rows = bus.call(SVC, OBJ, "org.freedesktop.systemd1.Manager", "ListUnits")[0]  # identifier: interface
        failed, timers = [], []
        for r in rows:
            name, _desc, _load, active, sub, _follow, opath = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
            if active == "failed" and len(failed) < MAX_FAILED:
                item = {"name": name[:120], "sub": sub[:24]}
                try:
                    item["since"] = _usec(bus.prop(opath, "org.freedesktop.systemd1.Unit", "StateChangeTimestamp"))  # identifier
                    if name.endswith(".service"):
                        item["result"] = str(bus.prop(opath, "org.freedesktop.systemd1.Service", "Result"))[:24]  # identifier
                except (OSError, BusError):
                    pass
                failed.append(item)
            if name.endswith(".timer") and len(timers) < MAX_TIMERS:
                item = {"name": name[:120], "active": active == "active"}
                try:
                    item["unit"] = str(bus.prop(opath, "org.freedesktop.systemd1.Timer", "Unit"))[:120]  # identifier
                    item["last"] = _usec(bus.prop(opath, "org.freedesktop.systemd1.Timer", "LastTriggerUSec"))  # identifier
                    item["next"] = _usec(bus.prop(opath, "org.freedesktop.systemd1.Timer", "NextElapseUSecRealtime"))  # identifier
                    item["result"] = str(bus.prop(opath, "org.freedesktop.systemd1.Timer", "Result"))[:24]  # identifier
                except (OSError, BusError):
                    pass
                timers.append(item)
        # the result of the unit each timer starts (a timer itself is "success" even when its service failed)
        by_name = {r[0]: r for r in rows}
        for t in timers:
            u = by_name.get(t.get("unit") or "")
            if u is None:
                continue
            t["unit_state"] = u[3][:16]
            if t["unit"].endswith(".service"):
                try:
                    t["unit_result"] = str(bus.prop(u[6], "org.freedesktop.systemd1.Service", "Result"))[:24]  # identifier
                except (OSError, BusError):
                    pass
        return {"available": True, "failed": failed, "timers": timers, "total": len(rows)}
    except (OSError, BusError, IndexError, ValueError, struct.error):
        return {"available": False, "reason": "error"}
    finally:
        bus.close()
