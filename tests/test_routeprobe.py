"""Network routes (DW2), the probe helper (lanloom/routeprobe.py): the Paris payload, reading the socket error queue, the UDP
and TCP tracers with a fake socket and a fake clock, reverse DNS with a deadline, the capability check and the request
loop of the helper process.

Nothing here sends a packet: every socket is a fake handed in through ``sock_factory``, ``socket.socket`` and the system
resolver are blocked for the whole module, and no subprocess may start."""
import errno
import io
import json
import select
import socket
import struct
import subprocess
import threading
import time

import pytest

from lanloom import routeprobe as rp

POLLERR, POLLOUT = select.POLLERR, select.POLLOUT
A1, A3, DEST = "198.51.100.1", "198.51.100.3", "203.0.113.9"


@pytest.fixture(autouse=True)
def nothing_real(monkeypatch):
    def no_socket(*a, **k):
        raise AssertionError("a real socket was opened")

    def no_run(*a, **k):
        raise AssertionError("a real process was started")
    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(socket, "gethostbyaddr", no_socket)
    monkeypatch.setattr(subprocess, "run", no_run)
    monkeypatch.setattr(subprocess, "Popen", no_run)


# ---------------------------------------------------------------------------------------------------------- payload
def test_paris_payload_constant_checksum_and_length():
    sums = {rp.ones_sum(rp.paris_payload(s)) for s in range(1, 65)}
    lens = {len(rp.paris_payload(s)) for s in range(1, 65)}
    assert len(sums) == 1 and len(lens) == 1
    assert rp.paris_payload(7)[:2] == struct.pack("!H", 7) and rp.paris_payload(7)[2:4] == struct.pack("!H", 7 ^ 0xFFFF)
    assert len({rp.paris_payload(s) for s in range(1, 65)}) == 64                    # but every probe is distinguishable
    assert rp.ones_sum(rp.paris_payload(0x1_0005)) == rp.ones_sum(rp.paris_payload(5))     # numbers wrap at 16 bits


def test_ones_sum_odd_length_and_known_value():
    assert rp.ones_sum(b"\x00\x01") == 1
    assert rp.ones_sum(b"\xff\xff\x00\x01") == 1                                     # the carry is folded back
    assert rp.ones_sum(b"\x01") == 0x0100


# ---------------------------------------------------------------------------------------------------------- error queue
def ee(origin, itype, icode, addr=None, v6=False, errno_=0):
    """Ancillary data of one error-queue message: struct sock_extended_err (16 bytes) + the offender's sockaddr."""
    head = struct.pack("=IBBBBII", errno_, origin, itype, icode, 0, 0, 0)
    assert len(head) == 16
    if addr is None:
        body = b""
    elif v6:
        body = struct.pack("=HHI", socket.AF_INET6, 0, 0) + socket.inet_pton(socket.AF_INET6, addr) + struct.pack("=I", 0)
    else:
        body = struct.pack("=HH", socket.AF_INET, 0) + socket.inet_pton(socket.AF_INET, addr) + b"\0" * 8
    level, ctype = (rp.SOL_IPV6, rp.IPV6_RECVERR) if v6 else (rp.SOL_IP, rp.IP_RECVERR)
    return [(level, ctype, head + body)]


@pytest.mark.parametrize("anc, expect", [
    (ee(2, 11, 0, A1), ("hop", A1)),                                                  # ICMP time exceeded
    (ee(2, 3, 3, DEST), ("reached", DEST)),                                           # ICMP port unreachable
    (ee(2, 3, 1, A3), ("unreach", A3)),                                               # host unreachable
    (ee(2, 12, 0, A3), None),                                                         # something else
    (ee(3, 3, 0, "2001:db8::1", v6=True), ("hop", "2001:db8::1")),                    # ICMPv6 time exceeded
    (ee(3, 1, 4, "2001:db8::9", v6=True), ("reached", "2001:db8::9")),                # ICMPv6 port unreachable
    (ee(3, 1, 0, "2001:db8::9", v6=True), ("unreach", "2001:db8::9")),
    (ee(0, 0, 0, None, errno_=errno.ECONNREFUSED), ("reached", None)),                # reset by the destination itself
    (ee(0, 0, 0, None, errno_=errno.ETIMEDOUT), None),
    (ee(2, 11, 0, None), ("hop", None)),                                              # the offender's address is missing
])
def test_parse_error(anc, expect):
    assert rp.parse_error(anc) == expect


def test_parse_error_short_garbage_and_foreign_levels():
    good = ee(2, 11, 0, A1)[0][2]
    assert rp.parse_error(None) is None and rp.parse_error([]) is None
    assert rp.parse_error([(rp.SOL_IP, rp.IP_RECVERR, b"")]) is None
    assert rp.parse_error([(rp.SOL_IP, rp.IP_RECVERR, good[:15])]) is None            # one byte too short
    rp.parse_error([(rp.SOL_IP, rp.IP_RECVERR, b"\xff" * 40)])                          # garbage never raises
    assert rp.parse_error([(1, 1, good)]) is None                                     # not an error-queue message
    assert rp.parse_error([(rp.SOL_IP, socket.IP_TTL, good)]) is None
    # a truncated offender address is not an address, the hop kind is still known
    assert rp.parse_error([(rp.SOL_IP, rp.IP_RECVERR, good[:16 + 6])]) == ("hop", None)
    # an IPv4 message first, then a good one: the first usable one counts
    assert rp.parse_error([(rp.SOL_IP, rp.IP_RECVERR, b"x" * 5)] + ee(2, 11, 0, A1)) == ("hop", A1)


# ---------------------------------------------------------------------------------------------------------- fakes
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeUDP:
    """A UDP socket on a scripted path: path[ttl] = {"kind": "hop"|"reached"|"silent"|"late", "addr": ..., "delay": s,
    "quote": "full"|"empty"} (a "late" answer shows up when the next probe is prepared)."""
    instances = []

    def __init__(self, path, clock, v6=False):
        self.path, self.clock, self.v6 = path, clock, v6
        self.queue, self.late = [], []
        self.ttl = None
        self.sent, self.opts, self.closed, self.connected = [], [], False, None
        self.fail_send = False
        FakeUDP.instances.append(self)

    def setsockopt(self, level, opt, value):
        self.opts.append((level, opt, value))
        if opt in (socket.IP_TTL, socket.IPV6_UNICAST_HOPS):
            self.ttl = value
            self.queue.extend(self.late)                          # answers that arrived since the last probe
            self.late = []

    def connect(self, addr):
        self.connected = addr

    def send(self, data):
        if self.fail_send:
            raise OSError(errno.ECONNREFUSED, "refused")
        self.sent.append((self.ttl, data))
        step = self.path.get(self.ttl) or {"kind": "silent"}
        if step["kind"] == "silent":
            return len(data)
        quoted = data if step.get("quote", "full") == "full" else b""
        if step["kind"] == "reached":
            anc = ee(3, 1, 4, step["addr"], v6=True) if self.v6 else ee(2, 3, 3, step["addr"])
        else:
            anc = ee(3, 3, 0, step["addr"], v6=True) if self.v6 else ee(2, 11, 0, step["addr"])
        item = (step.get("delay", 0.01), quoted, anc)
        (self.late if step["kind"] == "late" else self.queue).append(item)
        return len(data)

    def recvmsg(self, bufsize, ancbufsize, flags):
        assert flags & rp.MSG_ERRQUEUE and flags & socket.MSG_DONTWAIT
        if not self.queue:
            raise BlockingIOError()
        delay, quoted, anc = self.queue.pop(0)
        self.clock.t += 0 if delay is None else 0                 # reading takes no time; the delay passed in the wait
        return quoted, anc, 0, None

    def fake_wait(self, events, timeout):
        if self.queue:
            self.clock.t += self.queue[0][0]
            return POLLERR
        self.clock.t += max(0.0, timeout)
        return 0

    def close(self):
        self.closed = True


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    FakeUDP.instances = []
    monkeypatch.setattr(rp, "_wait", lambda s, ev, to: s.fake_wait(ev, to))
    return c


def udp(path, clock, **kw):
    v6 = ":" in kw.get("target", DEST)
    kw.setdefault("target", DEST)
    target = kw.pop("target")
    box = []

    def factory(fam, kind):
        assert kind == socket.SOCK_DGRAM and fam == (socket.AF_INET6 if v6 else socket.AF_INET)
        s = FakeUDP(path, clock, v6)
        box.append(s)
        return s
    hops, probes = rp.trace_udp(target, sock_factory=factory, clock=clock, **kw)
    return hops, probes, box[0]


PATH4 = {1: {"kind": "hop", "addr": "192.168.1.1", "delay": 0.0004}, 2: {"kind": "silent"},
         3: {"kind": "hop", "addr": A3, "delay": 0.012}, 4: {"kind": "reached", "addr": DEST, "delay": 0.02}}


# ---------------------------------------------------------------------------------------------------------- UDP tracer
def test_udp_four_hops_second_silent_destination_last(clock):
    hops, probes, s = udp(PATH4, clock, wait=1.0, gap=4)
    assert [h["ttl"] for h in hops] == [1, 2, 3, 4]
    assert [h["ip"] for h in hops] == ["192.168.1.1", None, A3, DEST]
    assert hops[1]["rtt"] is None
    assert hops[0]["rtt"] == pytest.approx(0.4, abs=0.01) and hops[2]["rtt"] == pytest.approx(12.0, abs=0.01)
    assert hops[3]["reached"] is True and not any(h.get("reached") for h in hops[:3])
    assert probes == 4 and s.closed
    assert s.connected == (DEST, rp.BASE_PORT)                                       # one destination port for every probe
    assert (rp.SOL_IP, rp.IP_RECVERR, 1) in s.opts
    # one TTL per probe, the payload keeps its checksum
    assert [t for t, _ in s.sent] == [1, 2, 3, 4]
    assert len({rp.ones_sum(d) for _, d in s.sent}) == 1 and len({len(d) for _, d in s.sent}) == 1


def test_udp_answers_that_quote_only_eight_bytes_belong_to_the_probe_in_flight(clock):
    path = {k: dict(v, quote="empty") for k, v in PATH4.items()}
    hops, probes, s = udp(path, clock)
    assert [h["ip"] for h in hops] == ["192.168.1.1", None, A3, DEST]
    assert hops[3]["reached"] is True and probes == 4


def test_udp_late_answer_with_its_number_is_credited_to_its_own_probe(clock):
    path = dict(PATH4)
    path[2] = {"kind": "late", "addr": "198.51.100.2", "delay": 0.0, "quote": "full"}
    hops, _, _ = udp(path, clock, wait=0.5)
    assert [h["ip"] for h in hops] == ["192.168.1.1", "198.51.100.2", A3, DEST]
    assert hops[1]["rtt"] >= 500.0                                                   # it took longer than the wait


def test_udp_late_answer_without_its_number_is_dropped(clock):
    path = dict(PATH4)
    path[2] = {"kind": "late", "addr": "198.51.100.2", "delay": 0.0, "quote": "empty"}
    hops, _, _ = udp(path, clock, wait=0.5)
    assert [h["ip"] for h in hops] == ["192.168.1.1", None, A3, DEST]               # nobody can tell whose answer it was


def test_udp_gap_limit_stops_after_n_silent_hops(clock):
    path = {1: {"kind": "hop", "addr": A1, "delay": 0.001}}
    hops, probes, _ = udp(path, clock, gap=3, max_hops=30)
    assert probes == 4 and [h["ttl"] for h in hops] == [1, 2, 3, 4]
    assert [h["ip"] for h in hops] == [A1, None, None, None]
    hops, probes, _ = udp(path, clock, gap=1, max_hops=30)
    assert probes == 2


def test_udp_a_hop_resets_the_gap_counter(clock):
    path = {1: {"kind": "silent"}, 2: {"kind": "silent"}, 3: {"kind": "hop", "addr": A1, "delay": 0.001},
            4: {"kind": "silent"}, 5: {"kind": "silent"}, 6: {"kind": "reached", "addr": DEST, "delay": 0.002}}
    hops, probes, _ = udp(path, clock, gap=3)
    assert probes == 6 and hops[-1]["ip"] == DEST and hops[-1]["reached"]


def test_udp_max_hops_and_the_hard_cap(clock):
    path = {i: {"kind": "hop", "addr": "198.51.100.%d" % i, "delay": 0.001} for i in range(1, 60)}
    hops, probes, _ = udp(path, clock, max_hops=3)
    assert probes == 3 and len(hops) == 3 and not any(h.get("reached") for h in hops)
    hops, probes, _ = udp(path, clock, max_hops=500)
    assert probes == rp.MAX_HOPS == len(hops)


def test_udp_budget_stops_the_measurement(clock):
    budget = rp.Budget(2.5, clock)
    hops, probes, _ = udp({}, clock, wait=1.0, gap=8, budget=budget)               # everything silent, 1 s per probe
    assert probes == 3 and len(hops) == 3 and all(h["ip"] is None for h in hops)
    assert clock.t - 1000.0 <= 2.5 + 1e-9                                            # never beyond the budget
    # a budget that is used up before the start sends nothing
    b = rp.Budget(1.0, clock)
    clock.t += 5
    hops, probes, s = udp(PATH4, clock, budget=b)
    assert probes == 0 and hops == [] and s.closed and s.sent == []


def test_udp_send_error_ends_the_run_but_closes_the_socket(clock):
    box = []

    def factory(fam, kind):
        s = FakeUDP(PATH4, clock)
        s.fail_send = True
        box.append(s)
        return s
    hops, probes = rp.trace_udp(DEST, sock_factory=factory, clock=clock)
    assert hops == [] and probes == 0 and box[0].closed


def test_udp_ipv6(clock):
    path = {1: {"kind": "hop", "addr": "2001:db8::1", "delay": 0.001}, 2: {"kind": "reached", "addr": "2001:db8::9", "delay": 0.002}}
    hops, probes, s = udp(path, clock, target="2001:db8::9")
    assert [h["ip"] for h in hops] == ["2001:db8::1", "2001:db8::9"] and hops[1]["reached"]
    assert (rp.SOL_IPV6, rp.IPV6_RECVERR, 1) in s.opts and (rp.SOL_IPV6, socket.IPV6_UNICAST_HOPS, 1) in s.opts


def test_udp_other_unreachable_ends_the_run_without_marking_the_destination(clock):
    class Unreach(FakeUDP):
        def send(self, data):
            self.sent.append((self.ttl, data))
            if self.ttl == 2:
                self.queue.append((0.01, data, ee(2, 3, 1, A3)))
            else:
                self.queue.append((0.01, data, ee(2, 11, 0, A1)))
            return len(data)
    hops, probes = rp.trace_udp(DEST, sock_factory=lambda f, k: Unreach({}, clock), clock=clock)
    assert probes == 2 and [h["ip"] for h in hops] == [A1, A3]
    assert not any("unreach" in h or h.get("reached") for h in hops)


# ---------------------------------------------------------------------------------------------------------- TCP tracer
class FakeTCP:
    """One TCP socket per probe. steps[ttl] = {"rc": connect_ex result, "ev": events of the wait, "anc": error-queue
    message or None, "so_error": int, "delay": s}."""
    instances = []

    def __init__(self, steps, clock):
        self.steps, self.clock = steps, clock
        self.ttl, self.opts, self.closed, self.bound, self.nonblocking = None, [], False, None, False
        self.refuse_bind_port = None
        FakeTCP.instances.append(self)

    def setsockopt(self, level, opt, value):
        self.opts.append((level, opt, value))
        if opt in (socket.IP_TTL, socket.IPV6_UNICAST_HOPS):
            self.ttl = value

    def bind(self, addr):
        if self.refuse_bind_port is not None and addr[1] == self.refuse_bind_port:
            raise OSError(errno.EADDRINUSE, "in use")
        self.bound = addr

    def getsockname(self):
        return (self.bound[0], self.bound[1] or 40000)

    def setblocking(self, flag):
        self.nonblocking = not flag

    @property
    def step(self):
        return self.steps.get(self.ttl) or {"rc": errno.EINPROGRESS, "ev": 0}

    def connect_ex(self, addr):
        self.addr = addr
        return self.step.get("rc", errno.EINPROGRESS)

    def fake_wait(self, events, timeout):
        st = self.step
        if st.get("ev"):
            self.clock.t += st.get("delay", 0.01)
            return st["ev"]
        self.clock.t += max(0.0, timeout)
        return 0

    def recvmsg(self, bufsize, ancbufsize, flags):
        anc = self.step.get("anc")
        if anc is None:
            raise BlockingIOError()
        self.clock.t += self.step.get("delay", 0.01) if self.step.get("rc") not in (None, errno.EINPROGRESS) else 0
        return b"", anc, 0, None

    def getsockopt(self, level, opt):
        return self.step.get("so_error", 0)

    def close(self):
        self.closed = True


def tcp(steps, clock, **kw):
    FakeTCP.instances = []
    hops, probes = rp.trace_tcp(kw.pop("target", DEST), sock_factory=lambda f, k: FakeTCP(steps, clock), clock=clock, **kw)
    return hops, probes, FakeTCP.instances


TCP4 = {1: {"ev": POLLERR, "anc": ee(2, 11, 0, "192.168.1.1"), "delay": 0.0005},
        2: {"ev": 0},
        3: {"ev": POLLERR, "anc": ee(2, 11, 0, A3), "delay": 0.012},
        4: {"ev": POLLOUT, "so_error": 0, "delay": 0.02}}


def test_tcp_four_hops_second_silent_destination_answers_the_handshake(clock):
    hops, probes, socks = tcp(TCP4, clock, wait=1.0, gap=4)
    assert [h["ip"] for h in hops] == ["192.168.1.1", None, A3, DEST]
    assert hops[3]["reached"] is True and hops[3]["rtt"] == pytest.approx(20.0, abs=0.01)
    assert probes == 4 and len(socks) == 4 and all(s.closed and s.nonblocking for s in socks)
    assert all(s.addr == (DEST, 443) for s in socks)
    assert [s.ttl for s in socks] == [1, 2, 3, 4]
    assert any(o[1] == socket.SO_LINGER for o in socks[3].opts)                     # the connection is reset at once
    assert socks[1].addr and hops[1]["rtt"] is None


def test_tcp_keeps_the_source_port_where_it_can(clock):
    hops, probes, socks = tcp(TCP4, clock)
    ports = [s.bound[1] for s in socks]
    assert ports[0] == 0 and set(ports[1:]) == {40000}                               # the first one picks, the rest reuse it


def test_tcp_falls_back_to_any_port_when_the_port_is_taken(clock):
    FakeTCP.instances = []

    def factory(f, k):
        s = FakeTCP(TCP4, clock)
        s.refuse_bind_port = 40000
        return s
    hops, probes = rp.trace_tcp(DEST, sock_factory=factory, clock=clock)
    assert hops[-1]["ip"] == DEST and probes == 4
    assert all(s.bound[1] == 0 for s in FakeTCP.instances)


def test_tcp_reset_by_the_destination_counts_as_reached(clock):
    steps = {1: {"ev": POLLERR, "anc": ee(2, 11, 0, A1)}, 2: {"ev": POLLERR, "anc": None, "so_error": errno.ECONNREFUSED, "delay": 0.01}}
    hops, probes, _ = tcp(steps, clock)
    assert [h["ip"] for h in hops] == [A1, DEST] and hops[1]["reached"] is True
    # an immediate refusal that carries an error-queue message
    steps = {1: {"rc": errno.ECONNREFUSED, "anc": ee(2, 3, 3, DEST)}}
    hops, probes, _ = tcp(steps, clock)
    assert hops == [{"ttl": 1, "ip": DEST, "rtt": hops[0]["rtt"], "reached": True}]


def test_tcp_port_unreachable_from_the_error_queue_counts_as_reached(clock):
    steps = {1: {"ev": POLLERR, "anc": ee(2, 3, 3, DEST)}}
    hops, probes, _ = tcp(steps, clock, port=8443)
    assert hops[0]["reached"] is True and probes == 1


def test_tcp_gap_limit_max_hops_and_budget(clock):
    hops, probes, _ = tcp({1: TCP4[1]}, clock, gap=2, wait=1.0)
    assert probes == 3 and [h["ip"] for h in hops] == ["192.168.1.1", None, None]
    steps = {i: {"ev": POLLERR, "anc": ee(2, 11, 0, "198.51.100.%d" % i), "delay": 0.001} for i in range(1, 60)}
    hops, probes, _ = tcp(steps, clock, max_hops=5)
    assert probes == 5 and len(hops) == 5 and not any(h.get("reached") for h in hops)
    hops, probes, _ = tcp(steps, clock, max_hops=999)
    assert probes == rp.MAX_HOPS
    clock.t = 1000.0
    hops, probes, socks = tcp({}, clock, wait=1.0, gap=8, budget=rp.Budget(2.5, clock))
    assert probes == 3 and all(h["ip"] is None for h in hops) and clock.t - 1000.0 <= 2.5 + 1e-9
    assert all(s.closed for s in socks)


def test_tcp_socket_errors_leave_a_silent_hop_and_close(clock):
    class Boom(FakeTCP):
        def setsockopt(self, *a):
            raise OSError(errno.EPERM, "no")
    FakeTCP.instances = []
    hops, probes = rp.trace_tcp(DEST, gap=2, sock_factory=lambda f, k: Boom({}, clock), clock=clock)
    assert [h["ip"] for h in hops] == [None, None] and probes == 0
    assert all(s.closed for s in FakeTCP.instances)


# ---------------------------------------------------------------------------------------------------------- reverse DNS
def test_lookup_names_with_a_fake_resolver():
    names = {A1: ("ae1.fra2.example.net", [], [A1]), A3: ("core1.newyork.example.invalid", [], [A3])}
    calls = []

    def resolver(ip):
        calls.append(ip)
        if ip in names:
            return names[ip]
        raise socket.herror(1, "unknown host")
    out = rp.lookup_names([A1, A3, "198.51.100.77", A1], deadline=2.0, resolver=resolver)
    assert out == {A1: "ae1.fra2.example.net", A3: "core1.newyork.example.invalid"}
    assert sorted(calls) == sorted([A1, A3, "198.51.100.77"])                         # every address once
    assert rp.lookup_names([], resolver=resolver) == {}
    assert rp.lookup_names(["198.51.100.8"], resolver=lambda ip: (_ for _ in ()).throw(UnicodeError("x"))) == {}
    assert rp.lookup_names(["198.51.100.8"], resolver=lambda ip: ()) == {}           # an empty answer is no name


def test_lookup_names_never_blocks_past_the_deadline():
    release = threading.Event()
    slow = "198.51.100.200"

    def resolver(ip):
        if ip == slow:
            release.wait(10)
        return ("fast.example.net", [], [ip])
    t0 = time.monotonic()
    try:
        out = rp.lookup_names([A1, slow], deadline=0.3, resolver=resolver)
        took = time.monotonic() - t0
    finally:
        release.set()
    assert out == {A1: "fast.example.net"}
    assert took < 1.5


def test_lookup_names_limits_the_number_of_addresses():
    seen = []
    ips = ["198.51.100.%d" % i for i in range(1, 200)]
    rp.lookup_names(ips, deadline=2.0, resolver=lambda ip: seen.append(ip) or ("x.example.net",))
    assert len(seen) <= rp.MAX_HOPS


def test_reverse_name_uses_the_system_resolver_only_without_a_fake(monkeypatch):
    monkeypatch.setattr(socket, "gethostbyaddr", lambda ip: ("host.example.net", [], [ip]))
    assert rp.reverse_name(A1) == "host.example.net"
    assert rp.reverse_name(A1, resolver=lambda ip: ("other.example.net",)) == "other.example.net"


# ---------------------------------------------------------------------------------------------------------- check
class OkSock:
    def __init__(self):
        self.opts, self.closed = [], False

    def setsockopt(self, *a):
        self.opts.append(a)

    def close(self):
        self.closed = True


def test_check_without_privileges_and_without_scamper(monkeypatch):
    monkeypatch.setattr(rp, "scamper_path", lambda: None)

    def denied(fam, kind):
        raise PermissionError(errno.EPERM, "no")
    res = rp.check(sock_factory=denied)
    assert res["methods"] == {"udp": False, "tcp": False, "scamper": False}
    assert res["reasons"]["udp"] == "socket:EPERM" and res["reasons"]["tcp"] == "socket:EPERM"
    assert res["reasons"]["scamper"] == "not_installed" and res["scamper"] is None
    json.dumps(res)


def test_check_everything_possible_and_the_socket_is_closed(monkeypatch):
    made = []
    monkeypatch.setattr(rp, "scamper_path", lambda: "/nonexistent/scamper")
    monkeypatch.setattr(rp, "scamper_version", lambda p: "scamper 20230000")
    monkeypatch.setattr(rp, "scamper_usable", lambda p: True)

    def ok(fam, kind):
        made.append(OkSock())
        return made[-1]
    res = rp.check(sock_factory=ok)
    assert res["methods"] == {"udp": True, "tcp": True, "scamper": True} and res["reasons"] == {} and res["scamper"] == "scamper 20230000"
    assert len(made) == 2 and all(m.closed for m in made)


def test_check_scamper_present_but_not_allowed_raw_sockets(monkeypatch):
    monkeypatch.setattr(rp, "scamper_path", lambda: "/nonexistent/scamper")
    monkeypatch.setattr(rp, "scamper_version", lambda p: None)
    res = rp.check(sock_factory=lambda f, k: OkSock())
    assert res["methods"]["scamper"] is False and res["reasons"]["scamper"] == "no_raw" and res["methods"]["udp"]
    monkeypatch.setattr(rp, "scamper_version", lambda p: "scamper 1")
    monkeypatch.setattr(rp, "scamper_usable", lambda p: False)
    res = rp.check(sock_factory=lambda f, k: OkSock())
    assert res["methods"]["scamper"] is False and res["scamper"] == "scamper 1"


def test_check_odd_socket_failures_never_raise(monkeypatch):
    monkeypatch.setattr(rp, "scamper_path", lambda: None)

    class NoOpt(OkSock):
        def setsockopt(self, *a):
            raise OSError(errno.ENOPROTOOPT, "no")
    res = rp.check(sock_factory=lambda f, k: NoOpt())
    assert res["methods"]["udp"] is False and res["reasons"]["udp"] == "socket:ENOPROTOOPT"

    def odd(f, k):
        raise AttributeError("no IP_RECVERR here")
    res = rp.check(sock_factory=odd)
    assert res["methods"]["udp"] is False and res["reasons"]["udp"] == "socket:unknown"


# ---------------------------------------------------------------------------------------------------------- measure
@pytest.mark.parametrize("target", ["", "example.net", "198.51.100.1; ls", "1.2.3", "198.51.100.1/24", "999.1.1.1", "localhost name", None])
def test_measure_rejects_a_target_that_is_not_an_address(target, monkeypatch):
    monkeypatch.setattr(rp, "trace_udp", lambda *a, **k: pytest.fail("measured a bad target"))
    monkeypatch.setattr(rp, "trace_tcp", lambda *a, **k: pytest.fail("measured a bad target"))
    for method in ("udp", "tcp", "scamper"):
        assert rp.measure({"method": method, "target": target}) == {"error": "target", "hops": [], "probes": 0}


def test_measure_unknown_method_and_scamper_missing(monkeypatch):
    assert rp.measure({"method": "icmp", "target": DEST}) == {"error": "method", "hops": [], "probes": 0}
    assert rp.measure({"target": DEST})["error"] == "method"
    monkeypatch.setattr(rp, "scamper_path", lambda: None)
    assert rp.measure({"method": "scamper", "target": DEST}) == {"method": "scamper", "error": "not_installed", "probes": 0}


def test_measure_scamper_hands_back_the_raw_output(monkeypatch):
    monkeypatch.setattr(rp, "scamper_path", lambda: "/nonexistent/scamper")
    seen = {}

    def fake(path, target, max_hops, wait, gap, budget_s):
        seen.update(target=target, max_hops=max_hops, wait=wait, gap=gap, budget=budget_s)
        return '{"type":"trace"}', None
    monkeypatch.setattr(rp, "run_scamper", fake)
    res = rp.measure({"method": "scamper", "target": DEST, "max_hops": 20, "wait": 1.0, "gap": 3, "budget": 20})
    assert res == {"method": "scamper", "raw": '{"type":"trace"}', "error": None, "probes": 20}
    assert seen["target"] == DEST and seen["max_hops"] == 20 and seen["gap"] == 3 and seen["budget"] == 19
    monkeypatch.setattr(rp, "run_scamper", lambda *a: (None, "timeout"))
    assert rp.measure({"method": "scamper", "target": DEST}) == {"method": "scamper", "raw": None, "error": "timeout", "probes": 0}


def test_run_scamper_reads_only_json_traces(monkeypatch):
    class R:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, b"", 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R(b'{"type":"trace","hops":[]}\n'))
    out, err = rp.run_scamper("/nonexistent/scamper", DEST, 5, 1, 2, 10)
    assert err is None and '"type":"trace"' in out
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R(b"permission denied\n"))
    assert rp.run_scamper("/nonexistent/scamper", DEST, 5, 1, 2, 10) == (None, "no_raw")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("scamper", 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    assert rp.run_scamper("/nonexistent/scamper", DEST, 5, 1, 2, 10) == (None, "timeout")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert rp.run_scamper("/nonexistent/scamper", DEST, 5, 1, 2, 10) == (None, "start")


def test_measure_udp_clamps_the_numbers_and_looks_up_names_for_public_routers_only(monkeypatch):
    seen = {}

    def fake_udp(target, max_hops, wait, gap, budget):
        seen.update(target=target, max_hops=max_hops, wait=wait, gap=gap, budget=budget)
        return [{"ttl": 1, "ip": "192.168.1.1", "rtt": 0.4}, {"ttl": 2, "ip": None, "rtt": None},
                {"ttl": 3, "ip": A1, "rtt": 5.0}, {"ttl": 4, "ip": DEST, "rtt": 9.0, "reached": True}], 4
    asked = []
    # the documentation ranges count as "private" for the standard library; only the home range is private in this test
    monkeypatch.setattr(rp, "_private", lambda ip: ip.startswith("192.168."))
    monkeypatch.setattr(rp, "trace_udp", fake_udp)
    monkeypatch.setattr(rp, "lookup_names", lambda ips, deadline: asked.append((list(ips), deadline)) or {A1: "ae1.fra2.example.net"})
    res = rp.measure({"method": "udp", "target": DEST, "max_hops": 999, "wait": 99, "gap": 99, "budget": 9999, "names": True})
    assert seen["max_hops"] == rp.MAX_HOPS and seen["wait"] == 3.0 and seen["gap"] == 8 and seen["budget"].left() <= 60.0
    assert asked[0][0] == [A1] and 0.5 <= asked[0][1] <= rp.NAMES_DEADLINE              # not the home router, not the destination
    assert res["error"] is None and res["method"] == "udp" and res["probes"] == 4
    assert [h.get("name") for h in res["hops"]] == [None, None, "ae1.fra2.example.net", None]
    rp.measure({"method": "udp", "target": DEST, "max_hops": 0, "wait": 0, "gap": 0, "budget": 0})
    assert seen["max_hops"] == 30 and seen["wait"] == 1.0 and seen["gap"] == 4              # empty values fall back to the defaults
    rp.measure({"method": "udp", "target": DEST, "max_hops": -5, "wait": -1, "gap": -2})
    assert seen["max_hops"] == 1 and seen["wait"] == 0.2 and seen["gap"] == 1
    asked.clear()
    rp.measure({"method": "udp", "target": DEST})                                             # no names wanted: no lookup
    assert asked == []


def test_measure_tcp_port_and_socket_error(monkeypatch):
    seen = {}
    monkeypatch.setattr(rp, "trace_tcp", lambda target, port, *a: seen.update(port=port) or ([], 0))
    rp.measure({"method": "tcp", "target": DEST, "port": 8443})
    assert seen["port"] == 8443
    rp.measure({"method": "tcp", "target": DEST, "port": 70000})
    assert seen["port"] == 443
    rp.measure({"method": "tcp", "target": DEST})
    assert seen["port"] == 443

    def boom(*a, **k):
        raise PermissionError(errno.EPERM, "no")
    monkeypatch.setattr(rp, "trace_udp", boom)
    assert rp.measure({"method": "udp", "target": DEST}) == {"method": "udp", "error": "socket:EPERM", "hops": [], "probes": 0}


def test_measure_end_to_end_with_a_fake_path(monkeypatch, clock):
    """The whole helper path with the fake sockets: from a request to JSON that routes.read_probe understands."""
    from lanloom import routes
    monkeypatch.setattr(rp, "open_socket", lambda fam, kind, factory=None: FakeUDP(PATH4, clock))
    res = rp.measure({"method": "udp", "target": DEST})
    assert res["error"] is None and res["probes"] == 4
    hops = routes.read_probe(json.dumps({"hops": res["hops"]}))
    assert [h["ip"] for h in hops] == ["192.168.1.1", None, A3, DEST] and hops[-1]["reached"]


# ---------------------------------------------------------------------------------------------------------- main
def run_main(monkeypatch, stdin_text):
    nice = []
    monkeypatch.setattr(rp.os, "nice", lambda n: nice.append(n) or 0)
    monkeypatch.setattr(rp.sys, "stdin", io.StringIO(stdin_text))
    out = io.StringIO()
    monkeypatch.setattr(rp.sys, "stdout", out)
    rp.main()
    assert nice == [19]                                                                      # the lowest priority first
    return json.loads(out.getvalue())


def test_main_reads_one_request_and_writes_one_answer(monkeypatch):
    monkeypatch.setattr(rp, "check", lambda: {"methods": {"udp": True}, "reasons": {}, "scamper": None})
    assert run_main(monkeypatch, '{"mode":"check"}')["methods"] == {"udp": True}
    assert run_main(monkeypatch, json.dumps({"mode": "measure", "target": "example.net", "method": "udp"})) == {"error": "target", "hops": [], "probes": 0}
    monkeypatch.setattr(rp, "trace_udp", lambda *a, **k: ([{"ttl": 1, "ip": A1, "rtt": 1.0}], 1))
    res = run_main(monkeypatch, json.dumps({"mode": "measure", "target": DEST, "method": "udp"}))
    assert res["hops"][0]["ip"] == A1 and res["error"] is None


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", '"check"', "null", '{"mode":"bogus"}', "{}", '{"mode": 5}', "\x00\xff"])
def test_main_bad_requests_get_a_mode_error(monkeypatch, text):
    monkeypatch.setattr(rp, "check", lambda: pytest.fail("checked on a bad request"))
    assert run_main(monkeypatch, text) == {"error": "mode"}


def test_main_reads_at_most_4096_bytes(monkeypatch):
    big = '{"mode":"check","pad":"' + "x" * 10000 + '"}'
    monkeypatch.setattr(rp, "check", lambda: pytest.fail("a cut request is no request"))
    assert run_main(monkeypatch, big) == {"error": "mode"}
