"""Host network source: netlink parser on constructed byte fixtures, byte conservation, flow cap, fallbacks.

Where netlink is available, an additional test reads the real kernel (own namespace, no privileges) while generating TCP traffic.
"""
import asyncio
import os
import random
import socket
import struct
import threading

import pytest

from lanloom.sources import hostnet as m

AF4, AF6 = socket.AF_INET, socket.AF_INET6


# ================================================================== fixtures (constructed bytes)
def _addr16(family, ip):
    raw = socket.inet_pton(family, ip)
    return raw + bytes(16 - len(raw))


def diag_msg(family, state, lip, lport, rip, rport, cookie, inode, acked=None, recv=None, seq=1, extra_attrs=True):
    body = struct.pack("=BBBB", family, state, 0, 0) + struct.pack(">HH", lport, rport)
    body += _addr16(family, lip) + _addr16(family, rip) + struct.pack("=I", 0) + struct.pack("=Q", cookie)
    body += struct.pack("=IIIII", 0, 0, 0, 1000, inode)
    assert len(body) == 72
    if extra_attrs:
        body += struct.pack("=HH", 4 + 16, 1) + bytes(16)               # INET_DIAG_MEMINFO, skipped
        body += struct.pack("=HH", 4 + 1, 5) + b"\x07" + bytes(3)       # odd length, padded
    if acked is not None:
        info = bytearray(232)
        struct.pack_into("=QQ", info, m.OFF_BYTES_ACKED, acked, recv)
        body += struct.pack("=HH", 4 + len(info), m.INET_DIAG_INFO) + bytes(info)
    return struct.pack("=IHHII", 16 + len(body), m.SOCK_DIAG_BY_FAMILY, 2, seq, 0) + body


def done_msg(seq=1):
    return struct.pack("=IHHII", 20, m.NLMSG_DONE, 2, seq, 0) + struct.pack("=i", 0)


def error_msg(errno_, seq=1):
    return struct.pack("=IHHII", 36, m.NLMSG_ERROR, 0, seq, 0) + struct.pack("=i", -errno_) + bytes(16)


def test_diag_request_layout():
    req = m.diag_request(AF6, seq=9)
    length, mtype, flags, seq, pid = struct.unpack_from("=IHHII", req)
    assert (length, mtype, flags, seq, pid) == (len(req), 20, 0x301, 9, 0)
    fam, proto, ext, pad, states = struct.unpack_from("=BBBBI", req, 16)
    assert (fam, proto, ext, pad) == (AF6, 6, 1 << (m.INET_DIAG_INFO - 1), 0)
    assert states == sum(1 << s for s in (1, 4, 5, 8, 9, 11))
    assert len(req) == 16 + 56


def test_parse_diag_v4_v6_mapped_and_done():
    buf = (diag_msg(AF4, 1, "192.0.2.10", 44000, "198.51.100.7", 443, 0xABC, 777, acked=1501, recv=9000)
           + diag_msg(AF6, 8, "2001:db8::1", 50000, "2001:db8::2", 22, 0xDEF, 778, acked=10, recv=20, extra_attrs=False)
           + diag_msg(AF6, 1, "::ffff:192.168.1.5", 1234, "::ffff:192.168.1.9", 80, 5, 0, acked=3, recv=4)
           + diag_msg(AF4, 6, "192.0.2.10", 1, "198.51.100.7", 2, 6, 0)                 # no tcp_info (TIME_WAIT)
           + done_msg())
    conns, done = m.parse_diag(buf)
    assert done
    assert conns == [
        {"lip": "192.0.2.10", "lport": 44000, "rip": "198.51.100.7", "rport": 443, "inode": 777, "key": 0xABC,
         "state": "ESTABLISHED", "acked": 1501, "recv": 9000},
        {"lip": "2001:db8::1", "lport": 50000, "rip": "2001:db8::2", "rport": 22, "inode": 778, "key": 0xDEF,
         "state": "CLOSE_WAIT", "acked": 10, "recv": 20},
        {"lip": "192.168.1.5", "lport": 1234, "rip": "192.168.1.9", "rport": 80, "inode": 0, "key": 5,
         "state": "ESTABLISHED", "acked": 3, "recv": 4},
    ]
    all_conns, _ = m.parse_diag(buf, include_unmetered=True)
    assert len(all_conns) == 4 and all_conns[3]["acked"] is None and all_conns[3]["state"] == "TIME_WAIT"


def test_parse_diag_partial_without_done_and_error():
    conns, done = m.parse_diag(diag_msg(AF4, 1, "10.0.0.1", 1, "10.0.0.2", 2, 1, 1, acked=1, recv=0))
    assert len(conns) == 1 and not done
    with pytest.raises(OSError):
        m.parse_diag(error_msg(13))
    assert m.parse_diag(error_msg(0)) == ([], True)


def test_parse_diag_truncated_and_garbage_never_crash():
    good = diag_msg(AF4, 1, "10.0.0.1", 1, "10.0.0.2", 2, 1, 1, acked=5, recv=6) + done_msg()
    for i in range(len(good)):
        conns, _ = m.parse_diag(good[:i])
        assert all(c["acked"] == 5 for c in conns)
    rnd = random.Random(11)
    for _ in range(3000):
        raw = bytes(rnd.randrange(256) for _ in range(rnd.randrange(400)))
        try:
            m.parse_diag(raw)
        except OSError:
            pass


# ================================================================== kernel TCP table fallback + interfaces
def _hex4(ip):
    return "%08X" % struct.unpack("=I", socket.inet_aton(ip))[0]


def _hex6(ip):
    raw = socket.inet_pton(AF6, ip)
    return "".join("%08X" % struct.unpack("=I", raw[i:i + 4])[0] for i in range(0, 16, 4))


TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def tcp_line(i, lip, lport, rip, rport, state, inode, v6=False):
    h = _hex6 if v6 else _hex4
    return "%4d: %s:%04X %s:%04X %02X 00000000:00000000 00:00000000 00000000  1000        0 %d 1 0 20 4 30 10 -1\n" % (
        i, h(lip), lport, h(rip), rport, state, inode)


def make_proc(tmp_path, dev_rx=1000, dev_tx=2000):
    net = tmp_path / "net"
    net.mkdir(exist_ok=True)
    (net / "tcp").write_text(TCP_HEADER + tcp_line(0, "0.0.0.0", 22, "0.0.0.0", 0, 0x0A, 10)
                             + tcp_line(1, "192.168.1.20", 22, "192.168.1.30", 51000, 0x01, 11)
                             + tcp_line(2, "127.0.0.1", 5000, "127.0.0.1", 41000, 0x01, 12)
                             + tcp_line(3, "192.168.1.20", 40000, "198.51.100.7", 443, 0x06, 0))
    (net / "tcp6").write_text(TCP_HEADER + tcp_line(0, "fd00::20", 8080, "fd00::31", 52000, 0x01, 13, v6=True)
                              + tcp_line(1, "::ffff:192.168.1.20", 9000, "::ffff:192.168.1.40", 53000, 0x08, 14, v6=True))
    (net / "dev").write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
        "    lo: 999 9 0 0 0 0 0 0 999 9 0 0 0 0 0 0\n"
        "  eth0: %d 10 0 0 0 0 0 0 %d 20 0 0 0 0 0 0\n"
        "vethab12: 5 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\n"
        "  br0: 7 1 0 0 0 0 0 0 7 1 0 0 0 0 0 0\n" % (dev_rx, dev_tx))
    sys_net = tmp_path / "sysnet"
    (sys_net / "br0" / "bridge").mkdir(parents=True, exist_ok=True)
    (sys_net / "eth0").mkdir(parents=True, exist_ok=True)
    return str(tmp_path), str(sys_net)


def test_tcp_table_parser(tmp_path):
    proc, _ = make_proc(tmp_path)
    conns = m.read_tcp_tables(proc)
    assert [(c["lip"], c["lport"], c["rip"], c["rport"], c["state"]) for c in conns] == [
        ("192.168.1.20", 22, "192.168.1.30", 51000, "ESTABLISHED"),
        ("127.0.0.1", 5000, "127.0.0.1", 41000, "ESTABLISHED"),
        ("fd00::20", 8080, "fd00::31", 52000, "ESTABLISHED"),
        ("192.168.1.20", 9000, "192.168.1.40", 53000, "CLOSE_WAIT"),
    ]
    assert all(c["acked"] is None for c in conns)
    assert m.read_tcp_tables(str(tmp_path / "missing")) is None


def test_iface_counter_deltas_skip_and_wrap(tmp_path):
    proc, sys_net = make_proc(tmp_path, dev_rx=2 ** 32 - 10, dev_tx=500)
    ic = m.IfaceCounter(proc, sys_net)
    stats = ic.read()
    assert set(stats) == {"eth0"}                          # lo, veth*, bridges skipped
    assert ic.deltas(stats) is None                        # baseline
    make_proc(tmp_path, dev_rx=20, dev_tx=800)
    assert ic.deltas(ic.read()) == [{"name": "eth0", "tx": 300, "rx": 30}]
    assert m.counter_delta(2 ** 40, 5) is None             # 64-bit decrease = reset


# ================================================================== byte conservation
def conn(key, acked, recv, lip="192.168.1.20", lport=40000, rip="198.51.100.7", rport=443):
    return {"lip": lip, "lport": lport, "rip": rip, "rport": rport, "inode": 1, "key": key, "state": "ESTABLISHED",
            "acked": acked, "recv": recv}


def test_byte_counter_rules():
    bc = m.ByteCounter()
    assert bc.update([conn(1, 1000, 5000)]) == ({}, [])                       # baseline only
    d, closed = bc.update([conn(1, 1500, 5100), conn(2, 1 + 300, 40)])
    assert d == {1: (500, 100), 2: (300, 40)}                                # new socket from SYN baseline
    # socket 2 vanished without notification: nothing more; socket 1 closed after the dump (in both)
    d, closed = bc.update([conn(1, 1600, 5100)], [conn(1, 1650, 5300), conn(3, 1 + 70, 7)])
    assert d == {1: (100, 0)}
    assert [(c["key"], a, b) for c, a, b in closed] == [(1, 50, 200), (3, 70, 7)]
    assert bc.last == {}                                                     # closed sockets forgotten
    # a destroy notification for a socket only known from the previous dump: remainder from last known value
    bc.update([conn(4, 1, 0)])
    bc.update([conn(4, 101, 10)])
    d, closed = bc.update([], [conn(4, 151, 30)])
    assert d == {} and [(a, b) for _, a, b in closed] == [(50, 20)]
    # counter going backwards is ignored
    bc.update([conn(5, 100, 100)])
    d, _ = bc.update([conn(5, 50, 100)])
    assert 5 not in d


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("destroy", [True, False])
def test_byte_conservation_random(seed, destroy):
    rnd = random.Random(seed)
    bc = m.ByteCounter()
    open_ = {}                  # key -> [tx, rx] true payload bytes
    next_key = [1]
    true_total = reported = lost = 0
    last_seen = {}              # key -> [tx, rx] at the last snapshot

    def new_socket():
        k = next_key[0]
        next_key[0] += 1
        open_[k] = [0, 0]
        return k

    def kernel(k, v):
        return conn(k, 1 + v[0], v[1])

    for step in range(120):
        counted = step > 0
        destroyed = []
        for k, v in open_.items():                               # 1. traffic
            a, b = rnd.randint(0, 5000), rnd.randint(0, 9000)
            v[0] += a
            v[1] += b
            true_total += (a + b) * counted
        for k in [k for k in open_ if rnd.random() < 0.08]:      # 2. close before the dump
            v = open_.pop(k)
            if destroy:
                destroyed.append(kernel(k, v))
            elif counted:
                base = last_seen.get(k, [0, 0])
                lost += v[0] - base[0] + v[1] - base[1]
        for _ in range(rnd.randint(0, 3)):                       # 3. short-lived sockets never seen in a dump
            v = [rnd.randint(0, 3000), rnd.randint(0, 3000)]
            true_total += sum(v) * counted
            if destroy:
                destroyed.append(kernel(next_key[0], v))
            elif counted:
                lost += sum(v)
            next_key[0] += 1
        for _ in range(rnd.randint(0, 4)):                       # 4. new sockets with first traffic
            k = new_socket()
            open_[k] = [rnd.randint(0, 2000), rnd.randint(0, 2000)]
            true_total += sum(open_[k]) * counted
        snapshot = [kernel(k, v) for k, v in open_.items()]      # 5. dump
        seen_now = {k: list(v) for k, v in open_.items()}
        for k in [k for k in open_ if rnd.random() < 0.05]:      # 6. more traffic, then close after the dump
            v = open_.pop(k)
            a, b = rnd.randint(1, 4000), rnd.randint(0, 4000)
            v[0] += a
            v[1] += b
            true_total += (a + b) * counted
            if destroy:
                destroyed.append(kernel(k, v))
            elif counted:
                lost += a + b
            seen_now.pop(k)
        deltas, closed = bc.update(snapshot, destroyed)
        if counted:
            reported += sum(a + b for a, b in deltas.values()) + sum(a + b for _, a, b in closed)
        last_seen = seen_now
    if destroy:
        assert reported == true_total
    else:
        assert reported == true_total - lost
        assert reported <= true_total


# ================================================================== flows
def test_build_flows_merge_loopback_and_unmetered():
    snap = [conn(1, 10, 10), conn(2, 10, 10, lip="127.0.0.1", rip="127.0.0.1"),
            conn(3, 10, 10, rip="0.0.0.0")]
    flows = m.build_flows(snap, {1: (5, 6), 2: (7, 8)}, [(conn(9, 0, 0), 1, 2)])
    assert flows == [{"local_ip": "192.168.1.20", "local_port": 40000, "peer_ip": "198.51.100.7", "peer_port": 443,
                      "tx": 6, "rx": 8}]                                  # merged with the closed socket
    assert len(m.build_flows(snap, {}, (), include_loopback=True)) == 2
    assert m.build_flows(snap[:1], None)[0]["tx"] is None


def test_cap_flows_keeps_busiest_and_conserves_bytes():
    rnd = random.Random(3)
    flows = [{"local_ip": "192.168.1.20", "local_port": 10000 + i, "peer_ip": "10.0.%d.%d" % (i % 7, i % 50),
              "peer_port": 443, "tx": rnd.randint(0, 10 ** 6), "rx": rnd.randint(0, 10 ** 6)} for i in range(5000)]
    out = m.cap_flows(list(flows), 2000)
    assert len(out) <= 2000
    assert sum(f["tx"] for f in out) == sum(f["tx"] for f in flows)
    assert sum(f["rx"] for f in out) == sum(f["rx"] for f in flows)
    singles = [f for f in out if "aggregated" not in f]
    biggest = sorted(flows, key=lambda f: -(f["tx"] + f["rx"]))[:len(singles)]
    assert singles == biggest
    assert all(f["peer_port"] is None for f in out if "aggregated" in f)
    assert sum(f.get("aggregated", 1) for f in out) == 5000


def test_cap_flows_more_peers_than_room():
    flows = [{"local_ip": "192.168.1.20", "local_port": i, "peer_ip": "10.%d.%d.1" % (i // 250, i % 250),
              "peer_port": 80, "tx": i, "rx": 1} for i in range(3000)]
    out = m.cap_flows(flows, 100)
    assert len(out) == 100
    assert sum(f["tx"] for f in out) == sum(f["tx"] for f in flows)
    assert out[-1]["peer_ip"] is None and sum(f.get("aggregated", 1) for f in out) == 3000


def test_cap_flows_without_bytes():
    flows = [{"local_ip": "a", "local_port": i, "peer_ip": "p%d" % (i % 3), "peer_port": 1, "tx": None, "rx": None}
             for i in range(50)]
    out = m.cap_flows(flows, 10)
    assert len(out) <= 10 and all(f["tx"] is None for f in out)


# ================================================================== source behaviour
class ScriptedReader:
    script = []

    def __init__(self, destroy=True):
        self.calls = 0

    def dump(self):
        snap, self.pending = ScriptedReader.script.pop(0)
        return snap

    def drain_destroyed(self):
        return self.pending

    def close(self):
        pass


def test_tick_netlink_mode_frames(tmp_path, monkeypatch):
    proc, sys_net = make_proc(tmp_path)
    ScriptedReader.script = [([conn(1, 100, 100)], []), ([conn(1, 400, 150)], [conn(2, 51, 9)])]
    monkeypatch.setattr(m, "NetlinkReader", ScriptedReader)
    h = m.HostNet(proc=proc, sys_net=sys_net)
    frames, st = h.tick()
    assert st == (True, "source.hostnet.ok")
    assert [f["type"] for f in frames] == ["bytes"]
    assert frames[0]["flows"][0]["tx"] == 0 and frames[0]["source"] == "host"
    frames, st = h.tick()
    assert [f["type"] for f in frames] == ["bytes", "iface"]
    assert frames[0]["flows"] == [{"local_ip": "192.168.1.20", "local_port": 40000, "peer_ip": "198.51.100.7",
                                   "peer_port": 443, "tx": 350, "rx": 59}]
    assert frames[1]["ifaces"] == [{"name": "eth0", "tx": 0, "rx": 0}]


def test_tick_fallback_without_netlink(tmp_path, monkeypatch):
    proc, sys_net = make_proc(tmp_path)

    def broken(destroy=True):
        raise OSError(97, "address family not supported")
    monkeypatch.setattr(m, "NetlinkReader", broken)
    h = m.HostNet(proc=proc, sys_net=sys_net)
    frames, st = h.tick()
    assert st == (True, "source.hostnet.no_bytes") and h.mode == "table"
    flows = frames[0]["flows"]
    assert {(f["peer_ip"], f["tx"], f["rx"]) for f in flows} == {
        ("192.168.1.30", None, None), ("fd00::31", None, None), ("192.168.1.40", None, None)}


def test_tick_nothing_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "NetlinkReader", lambda destroy=True: (_ for _ in ()).throw(OSError()))
    h = m.HostNet(proc=str(tmp_path / "none"), sys_net=str(tmp_path))
    frames, st = h.tick()
    assert frames == [] and st == (False, "source.hostnet.unavailable")


def test_run_unsupported_platform_stops(monkeypatch):
    monkeypatch.setattr(m, "supported", lambda: False)
    events = []
    asyncio.run(asyncio.wait_for(m.HostNet().run(events.append), 2))
    assert events == [{"type": "status", "source": "host", "ok": False, "key": "source.hostnet.unsupported",
                       "t": events[0]["t"]}]
    assert asyncio.run(m.HostNet().test()) == (False, "source.hostnet.unsupported", {})


def test_run_loop_emits_and_cancels(tmp_path, monkeypatch):
    proc, sys_net = make_proc(tmp_path)
    monkeypatch.setattr(m, "supported", lambda: True)
    ScriptedReader.script = [([conn(1, 100 * i, 0)], []) for i in range(1, 50)]
    monkeypatch.setattr(m, "NetlinkReader", ScriptedReader)
    events = []

    async def go():
        task = asyncio.create_task(m.HostNet(interval=0.2, proc=proc, sys_net=sys_net).run(events.append))
        await asyncio.sleep(0.75)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    kinds = [e["type"] for e in events]
    assert kinds.count("bytes") >= 3 and "iface" in kinds and kinds.count("status") == 1
    txs = [e["flows"][0]["tx"] for e in events if e["type"] == "bytes"]
    assert txs[0] == 0 and all(t == 100 for t in txs[1:])


# ================================================================== real kernel (where netlink exists)
@pytest.mark.skipif(not m.supported(), reason="netlink socket diagnostics not available on this platform")
def test_real_kernel_counts_local_tcp_bytes():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    cli = socket.create_connection(("127.0.0.1", port))
    peer, _ = srv.accept()
    h = m.HostNet(include_loopback=True)
    try:
        frames, st = h.tick()
        assert st == (True, "source.hostnet.ok"), st
        cli.sendall(b"x" * 300_000)
        got = 0
        while got < 300_000:
            got += len(peer.recv(65536))
        peer.sendall(b"y" * 1234)
        cli.recv(65536)
        frames, _ = h.tick()
        flows = next(f["flows"] for f in frames if f["type"] == "bytes")
        mine = [f for f in flows if f["peer_port"] == port and f["peer_ip"] == "127.0.0.1"]
        assert mine and mine[0]["tx"] == 300_000 and mine[0]["rx"] == 1234
    finally:
        h.close()
        for s in (cli, peer, srv):
            s.close()
