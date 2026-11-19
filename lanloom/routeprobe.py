"""Route probe helper (DW2): measures the hops towards one destination. Runs as its own short-lived process.

Started by ``routewatch.py`` as ``<python> -I <this file>`` with lowest CPU priority (nice 19), an empty environment and a
hard time limit; it reads one JSON request from stdin and writes one JSON answer to stdout. It imports nothing but the
standard library, gets no access to the database, the settings or any secret, and never writes a file. The destination
address arrives on stdin only (never on a command line, where other users of the host could read it) and is gone when
the process ends.

Methods (the caller picks the first one that is possible, see ``check``):
  * ``scamper``: Paris traceroute (UDP, the same flow identifier for every probe of a measurement) by the external
    ``scamper`` program, only if it is installed and may open raw sockets (raw sockets need the NET_RAW capability, see
    docs/INSTALL.md). Its output is parsed by ``routes.read_scamper`` in the caller.
  * ``udp``: Paris traceroute over an ordinary UDP socket, no privileges at all (the method of the classic Linux
    traceroute without root): the time-to-live of each probe is set on the socket, and the kernel hands the ICMP "time
    exceeded" / "port unreachable" answers to the socket's error queue (IP_RECVERR / IPV6_RECVERR) together with the
    address of the router that sent them. Source and destination port stay the same for the whole measurement, and the
    payload is built so that the UDP checksum stays the same too (the probe number and its ones' complement), so load
    balancers keep every probe on one path.
  * ``tcp``: the same over TCP: a connection attempt (SYN) with a small time-to-live to an open port (443 by default), one
    attempt per hop from the same source port where the system allows it; a SYN that reaches the destination is answered
    and closed at once.
Per hop one probe, at most ``max_hops`` hops, stop after ``gap`` silent hops in a row or at the destination, and never
longer than ``budget`` seconds. Reverse DNS names of the routers (for places and exchange points in the names) are looked
up at the end with a short overall deadline and only returned to the caller, which never stores them.

Request:  {"mode": "check"} | {"mode": "measure", "method": "udp"|"tcp"|"scamper", "target": "<address>", "port": 443,
           "max_hops": 30, "wait": 1.0, "gap": 4, "budget": 25, "names": true}
Answer:   check   -> {"methods": {"scamper": bool, "udp": bool, "tcp": bool}, "reasons": {...}, "scamper": "<version>"|null}
          measure -> {"method": ..., "hops": [{"ttl", "ip", "rtt", "name", "reached"?}], "probes": n, "error": null|"<code>"}
                     (scamper: {"method": "scamper", "raw": "<its JSON output>", "probes": n, "error": ...})
"""
import errno
import ipaddress
import json
import os
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

SOL_IP = 0
SOL_IPV6 = 41
IP_RECVERR = 11
IPV6_RECVERR = 25
MSG_ERRQUEUE = 0x2000
ORIGIN_ICMP, ORIGIN_ICMP6 = 2, 3
BASE_PORT = 33434                 # destination port of the UDP probes (the usual traceroute port), constant per measurement
PAYLOAD = b"lanloom route probe"  # the rest of every UDP probe (the length stays the same)
MAX_HOPS = 32
SCAMPER_MAX_OUT = 256 * 1024
NAMES_DEADLINE = 3.0


def _family(ip):
    return socket.AF_INET6 if ipaddress.ip_address(ip).version == 6 else socket.AF_INET


def _ttl_option(fam):
    return (SOL_IPV6, socket.IPV6_UNICAST_HOPS) if fam == socket.AF_INET6 else (SOL_IP, socket.IP_TTL)


def _recverr_option(fam):
    return (SOL_IPV6, IPV6_RECVERR) if fam == socket.AF_INET6 else (SOL_IP, IP_RECVERR)


def paris_payload(seq):
    """Probe number + its ones' complement + fixed rest: the 16-bit ones' complement sum of the payload is the same for
    every probe, so the UDP checksum (and with it every header field a load balancer may look at) does not change."""
    seq &= 0xFFFF
    return struct.pack("!HH", seq, seq ^ 0xFFFF) + PAYLOAD


def ones_sum(data):
    """16-bit ones' complement sum (used by the tests to prove the constant checksum)."""
    if len(data) % 2:
        data += b"\0"
    s = 0
    for (w,) in struct.iter_unpack("!H", data):
        s += w
        s = (s & 0xFFFF) + (s >> 16)
    return s


def parse_error(ancdata):
    """Ancillary data of a MSG_ERRQUEUE read -> (kind, offender address) or None.
    kind: "hop" (time exceeded), "reached" (port unreachable / refused at the destination), "unreach" (other unreachable)."""
    for level, ctype, data in ancdata or ():
        if not ((level == SOL_IP and ctype == IP_RECVERR) or (level == SOL_IPV6 and ctype == IPV6_RECVERR)):
            continue
        if len(data) < 16:
            continue
        ee_errno, origin, itype, icode = struct.unpack_from("=IBBB", data, 0)
        addr = None
        off = data[16:]
        if len(off) >= 8:
            fam = struct.unpack_from("=H", off, 0)[0]
            try:
                if fam == socket.AF_INET and len(off) >= 8:
                    addr = socket.inet_ntop(socket.AF_INET, off[4:8])
                elif fam == socket.AF_INET6 and len(off) >= 24:
                    addr = socket.inet_ntop(socket.AF_INET6, off[8:24])
            except (OSError, ValueError):
                addr = None
        if origin == ORIGIN_ICMP:
            kind = "hop" if itype == 11 else "reached" if (itype == 3 and icode == 3) else "unreach" if itype == 3 else None
        elif origin == ORIGIN_ICMP6:
            kind = "hop" if itype == 3 else "reached" if (itype == 1 and icode == 4) else "unreach" if itype == 1 else None
        else:
            kind = "reached" if ee_errno == errno.ECONNREFUSED else None
        if kind:
            return kind, addr
    return None


class Budget:
    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.end = clock() + max(1.0, float(seconds))

    def left(self):
        return self.end - self.clock()


def _wait(sock, events, timeout):
    p = select.poll()
    p.register(sock, events)
    r = p.poll(max(0, int(timeout * 1000)))
    return r[0][1] if r else 0


def trace_udp(target, max_hops=30, wait=1.0, gap=4, budget=None, sock_factory=socket.socket, clock=time.monotonic):
    """Paris UDP over the error queue. Returns (hops, probes)."""
    fam = _family(target)
    budget = budget or Budget(30, clock)
    s = sock_factory(fam, socket.SOCK_DGRAM)
    hops, probes, silent = [], 0, 0
    try:
        s.setsockopt(*_recverr_option(fam), 1)
        s.connect((target, BASE_PORT))
        lvl, opt = _ttl_option(fam)
        sent = {}
        for ttl in range(1, min(max_hops, MAX_HOPS) + 1):
            if budget.left() <= 0:
                break
            s.setsockopt(lvl, opt, ttl)
            _drain(s, sent, hops, clock)              # late answers of earlier probes (only those carrying their number count)
            sent[ttl] = clock()
            try:
                s.send(paris_payload(ttl))
            except OSError:
                _drain(s, sent, hops, clock)          # an older answer made the send report it: read it and retry once
                sent[ttl] = clock()
                try:
                    s.send(paris_payload(ttl))
                except OSError:
                    break
            probes += 1
            got = None
            end = min(clock() + wait, budget.end)
            while got is None and clock() < end:
                if not _wait(s, select.POLLERR, end - clock()) & select.POLLERR:
                    break
                for item in _drain(s, sent, hops, clock, ttl):
                    if item["ttl"] == ttl:
                        got = item
            if got is None:
                hops.append({"ttl": ttl, "ip": None, "rtt": None})
                silent += 1
                if silent >= gap:
                    break
                continue
            silent = 0
            if got.get("reached") or got.get("unreach"):
                break
    finally:
        s.close()
    hops = _merge(hops)
    return hops, probes


def _drain(s, sent, hops, clock, current=None):
    """Reads every waiting error of the socket; each one that matches a probe becomes a hop (added to ``hops``).
    Many routers quote only the first 8 bytes of the UDP header in their answer, so the probe number in the payload is
    missing: such an answer belongs to the one probe in flight (``current``; only one probe is ever waiting), and is
    dropped when nothing is waiting."""
    out = []
    while True:
        try:
            data, anc, _flags, _addr = s.recvmsg(512, 512, MSG_ERRQUEUE | socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            break
        except OSError:
            break
        res = parse_error(anc)
        if res is None:
            continue
        seq = None
        if len(data) >= 4:
            seq, comp = struct.unpack_from("!HH", data, 0)
            if comp != seq ^ 0xFFFF:
                seq = None
        if seq is None and len(data) < 4:
            seq = current
        if seq is None or seq not in sent:
            continue
        kind, addr = res
        hop = {"ttl": seq, "ip": addr, "rtt": round((clock() - sent[seq]) * 1000.0, 3)}
        if kind == "reached":
            hop["reached"] = True
        elif kind == "unreach":
            hop["unreach"] = True
        hops.append(hop)
        out.append(hop)
    return out


def _merge(hops):
    """One entry per ttl (the answered one wins), sorted; nothing after the destination."""
    by = {}
    for h in hops:
        e = by.get(h["ttl"])
        if e is None or (e.get("ip") is None and h.get("ip")):
            by[h["ttl"]] = h
    out = []
    for t in sorted(by):
        h = dict(by[t])
        h.pop("unreach", None)
        out.append(h)
        if h.get("reached"):
            break
    return out


def trace_tcp(target, port=443, max_hops=30, wait=1.0, gap=4, budget=None, sock_factory=socket.socket, clock=time.monotonic):
    """Connection attempts with a growing time-to-live; the routers' answers come through the error queue."""
    fam = _family(target)
    budget = budget or Budget(30, clock)
    hops, probes, silent, sport = [], 0, 0, 0
    lvl, opt = _ttl_option(fam)
    for ttl in range(1, min(max_hops, MAX_HOPS) + 1):
        if budget.left() <= 0:
            break
        s = sock_factory(fam, socket.SOCK_STREAM)
        hop = {"ttl": ttl, "ip": None, "rtt": None}
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("::" if fam == socket.AF_INET6 else "0.0.0.0", sport))
            except OSError:
                s.bind(("::" if fam == socket.AF_INET6 else "0.0.0.0", 0))
            sport = s.getsockname()[1]
            s.setsockopt(*_recverr_option(fam), 1)
            s.setsockopt(lvl, opt, ttl)
            s.setblocking(False)
            t0 = clock()
            rc = s.connect_ex((target, port))
            probes += 1
            if rc not in (0, errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK):
                res = _tcp_error(s)
                if res:
                    hop.update(ip=res[1], rtt=round((clock() - t0) * 1000.0, 3), **({"reached": True} if res[0] == "reached" else {}))
            else:
                ev = _wait(s, select.POLLOUT | select.POLLERR, max(0.0, min(wait, budget.left())))
                if ev & select.POLLERR:
                    res = _tcp_error(s)
                    if res:
                        hop.update(ip=res[1] or target, rtt=round((clock() - t0) * 1000.0, 3))
                        if res[0] == "reached":
                            hop["reached"] = True
                    else:
                        err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        if err == errno.ECONNREFUSED:            # the destination answered with a reset: reached
                            hop.update(ip=target, rtt=round((clock() - t0) * 1000.0, 3), reached=True)
                elif ev & select.POLLOUT and s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    hop.update(ip=target, rtt=round((clock() - t0) * 1000.0, 3), reached=True)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # close at once (reset)
        except OSError:
            pass
        finally:
            s.close()
        hops.append(hop)
        if hop.get("reached"):
            break
        silent = 0 if hop["ip"] else silent + 1
        if silent >= gap:
            break
    return _merge(hops), probes


def _tcp_error(s):
    try:
        _data, anc, _f, _a = s.recvmsg(512, 512, MSG_ERRQUEUE | socket.MSG_DONTWAIT)
    except OSError:
        return None
    return parse_error(anc)


def lookup_names(ips, deadline=NAMES_DEADLINE, resolver=socket.gethostbyaddr):
    """Reverse DNS for the routers, in parallel, with one overall deadline; what is not back by then has no name."""
    out, threads = {}, []

    def one(ip):
        try:
            out[ip] = resolver(ip)[0]
        except (OSError, UnicodeError, IndexError):
            pass
    for ip in list(dict.fromkeys(ips))[:MAX_HOPS]:
        t = threading.Thread(target=one, args=(ip,), daemon=True)
        t.start()
        threads.append(t)
    end = time.monotonic() + deadline
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    return dict(out)


def _private(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return a.is_private or a.is_loopback or a.is_link_local


# ---------------------------------------------------------------------------------------------------------- scamper
def scamper_path():
    return shutil.which("scamper", path="/usr/local/bin:/usr/bin:/bin")


def scamper_version(path):
    try:
        r = subprocess.run([path, "-v"], capture_output=True, timeout=5, env={}, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (r.stdout or r.stderr or b"").decode("ascii", "replace").strip()
    return text[:60] if r.returncode == 0 and text else None


def run_scamper(path, target, max_hops, wait, gap, budget_s):
    cmd = [path, "-O", "json", "-c", "trace -P udp-paris -q 1 -w %d -m %d -g %d" % (max(1, int(round(wait))), max_hops, gap), "-f", "-"]
    try:
        r = subprocess.run(cmd, input=(target + "\n").encode(), capture_output=True, timeout=max(2.0, budget_s), env={}, check=False)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "start"
    out = (r.stdout or b"")[:SCAMPER_MAX_OUT].decode("utf-8", "replace")
    if '"type":"trace"' not in out.replace(" ", ""):
        return None, "no_raw"
    return out, None


def scamper_usable(path):
    """True if scamper runs and may open raw sockets: a trace to the loopback address (no packet leaves this machine)."""
    out, err = run_scamper(path, "127.0.0.1", 1, 1, 1, 6)
    return out is not None and err is None


# ---------------------------------------------------------------------------------------------------------- check / measure
def check(sock_factory=socket.socket):
    methods, reasons = {}, {}
    for name, kind in (("udp", socket.SOCK_DGRAM), ("tcp", socket.SOCK_STREAM)):
        try:
            s = sock_factory(socket.AF_INET, kind)
            try:
                s.setsockopt(SOL_IP, IP_RECVERR, 1)
                s.setsockopt(SOL_IP, socket.IP_TTL, 1)
            finally:
                s.close()
            methods[name] = True
        except (OSError, AttributeError) as exc:
            methods[name] = False
            reasons[name] = "socket:%s" % (errno.errorcode.get(getattr(exc, "errno", 0) or 0, "unknown"),)
    path = scamper_path()
    version = None
    if not path:
        methods["scamper"] = False
        reasons["scamper"] = "not_installed"
    else:
        version = scamper_version(path)
        ok = bool(version) and scamper_usable(path)
        methods["scamper"] = ok
        if not ok:
            reasons["scamper"] = "no_raw"
    return {"methods": methods, "reasons": reasons, "scamper": version}


def measure(req):
    target = str(req.get("target") or "")
    try:
        ipaddress.ip_address(target)
    except ValueError:
        return {"error": "target", "hops": [], "probes": 0}
    method = req.get("method")
    max_hops = max(1, min(int(req.get("max_hops") or 30), MAX_HOPS))
    wait = max(0.2, min(float(req.get("wait") or 1.0), 3.0))
    gap = max(1, min(int(req.get("gap") or 4), 8))
    budget_s = max(2.0, min(float(req.get("budget") or 25), 60.0))
    budget = Budget(budget_s)
    if method == "scamper":
        path = scamper_path()
        if not path:
            return {"method": "scamper", "error": "not_installed", "probes": 0}
        raw, err = run_scamper(path, target, max_hops, wait, gap, budget_s - 1)
        return {"method": "scamper", "raw": raw, "error": err, "probes": max_hops if raw else 0}
    try:
        if method == "tcp":
            port = int(req.get("port") or 443)
            hops, probes = trace_tcp(target, port if 1 <= port <= 65535 else 443, max_hops, wait, gap, budget)
        elif method == "udp":
            hops, probes = trace_udp(target, max_hops, wait, gap, budget)
        else:
            return {"error": "method", "hops": [], "probes": 0}
    except OSError as exc:
        return {"method": method, "error": "socket:%s" % errno.errorcode.get(exc.errno or 0, "unknown"), "hops": [], "probes": 0}
    if req.get("names"):
        names = lookup_names([h["ip"] for h in hops if h.get("ip") and not _private(h["ip"]) and h["ip"] != target],
                             max(0.5, min(NAMES_DEADLINE, budget.left())))
        for h in hops:
            if h.get("ip") in names:
                h["name"] = names[h["ip"]]
    return {"method": method, "hops": hops, "probes": probes, "error": None}


def main():
    try:
        os.nice(19)
    except (OSError, AttributeError):
        pass
    try:
        req = json.loads(sys.stdin.read(4096) or "{}")
    except ValueError:
        req = {}
    if not isinstance(req, dict):
        req = {}
    if req.get("mode") == "check":
        out = check()
    elif req.get("mode") == "measure":
        out = measure(req)
    else:
        out = {"error": "mode"}
    sys.stdout.write(json.dumps(out, separators=(",", ":")))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
