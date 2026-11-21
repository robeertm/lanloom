"""Replay of the recorded NetFlow v5 / v9 / IPFIX sample packets (tests/fixtures/netflow, made up in tests/netflow_samples.py).

1. The files are exactly what the generator writes (nobody edits a fixture by hand, regenerating gives the same bytes).
2. Replayed into the collector, every exporter shows what the recording expects: templates per exporter, data before its
   template waits, options records are skipped (their sampling rate is used), a withdrawn template, a v9 restart, a cut-off
   packet, a sender that is not allowed and is offered in the settings.
3. Replayed through a real UDP socket into the core (one loopback address per exporter), the data paths per device match.
"""
import asyncio
import ipaddress
import json
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netflow_samples as ns  # noqa: E402

from lanloom import netflow  # noqa: E402
from lanloom.store import Store  # noqa: E402

DIR = ns.DIR
REC = json.loads((DIR / "recording.json").read_text(encoding="utf-8"))
OUTSIDE = [ipaddress.ip_network(n) for n in ("8.8.8.0/24", "9.9.9.0/24", "2620:fe::/48")]


def _load():
    return [(p["second"], p["sender"], (DIR / p["file"]).read_bytes()) for p in REC["packets"]]


def test_fixture_files_match_the_generator(tmp_path):
    ns.write(tmp_path)
    made = sorted(p.name for p in tmp_path.iterdir())
    assert made == sorted(p.name for p in DIR.iterdir()), "run: python3 -I tests/netflow_samples.py --write"
    for name in made:
        assert (tmp_path / name).read_bytes() == (DIR / name).read_bytes(), name
    assert {p["sender"] for p in REC["packets"]} == {ns.ROUTER, ns.FIREWALL, ns.OLDBOX, ns.STRANGER}
    for p in REC["packets"]:                                         # nothing but made-up and documentation addresses
        assert ipaddress.ip_address(p["sender"]) in ipaddress.ip_network("192.0.2.0/24")


class Paths:
    def __init__(self):
        self.calls, self.pairs = [], []

    def book(self, device, peer, tx, rx, now):
        self.calls.append((device, peer, tx, rx))
        return True

    def book_pair(self, a, b, size, now):
        self.pairs.append((a, b, size))
        return True


def _check(st, expect):
    for ip, want in expect["exporters"].items():
        got = st["exporters"][ip]
        assert {k: got[k] for k in want} == want, (ip, got)
    assert st["dropped_foreign"] == expect["dropped_foreign"]
    assert [r["ip"] for r in st["refused_senders"]] == expect["refused_senders"]
    assert st["skipped_self"] == expect["skipped_self"]


def test_replay_into_the_collector():
    t = [1_790_000_000.0]
    paths = Paths()
    col = netflow.Collector(paths, REC["devices"].get, lambda: [ipaddress.ip_network(REC["lan"])],
                            skip_ips=lambda: {REC["self"]}, clock=lambda: t[0],
                            outside=lambda ip: any(ip.version == n.version and ip in n for n in OUTSIDE))
    col.configure([ns.ROUTER, ns.FIREWALL, ns.OLDBOX], 2055)
    start = t[0]
    for second, sender, data in _load():
        t[0] = start + second
        col.handle(data, sender)
    st = col.stats()
    _check(st, REC["expect"])
    sums: dict = {}
    for dev, _peer, tx, rx in paths.calls:
        s = sums.setdefault(dev, {"tx": 0, "rx": 0})
        s["tx"] += tx
        s["rx"] += rx
    assert sums == REC["expect"]["devices"]
    assert ("dev-laptop", "dev-printer", 4_000) in paths.pairs and ("dev-phone", "dev-tv", 300) in paths.pairs
    # no peer address, port or interface name is kept in the figures
    blob = json.dumps(st)
    for text in ("8.8.8.8", "9.9.9.9", "2620:fe::fe", "lan0", "443"):
        assert text not in blob


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


LOOP = {ns.ROUTER: "127.0.0.1", ns.FIREWALL: "127.0.0.2", ns.OLDBOX: "127.0.0.3", ns.STRANGER: "127.0.0.9"}


def _can_send_from_loopback_aliases():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.2", 0))
        s.close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _can_send_from_loopback_aliases(), reason="this system cannot send from 127.0.0.2")
def test_replay_through_a_real_socket_into_the_core(tmp_path):
    from lanloom.core import Core
    c = Core(SimpleNamespace(data_dir=tmp_path, demo=False, probe=False), Store(":memory:"), None)
    c.geo.start(background=False)
    c.ip_to_device = {k: v for k, v in REC["devices"].items() if ":" not in k}
    c.ip6_to_device = {k: v for k, v in REC["devices"].items() if ":" in k}
    c.self_ip = REC["self"]
    c._nf_lan = (time.time(), [ipaddress.ip_network(REC["lan"])])
    port = _free_udp_port()
    c.update_settings({"netflow_enabled": True, "netflow_port": port,
                       "netflow_exporters": [LOOP[ns.ROUTER], LOOP[ns.FIREWALL], LOOP[ns.OLDBOX]]})
    total = len(REC["packets"])

    async def run():
        await c.netflow_apply()
        assert c.netflow.stats()["listening"], c.netflow.stats()
        socks = {}
        try:
            for _second, sender, data in _load():
                s = socks.get(sender)
                if s is None:
                    s = socks[sender] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.bind((LOOP[sender], 0))
                s.sendto(data, ("127.0.0.1", port))
                await asyncio.sleep(0.02)                            # keep the recorded order
        finally:
            for s in socks.values():
                s.close()
        for _ in range(250):
            st = c.netflow.stats()
            if st["packets"] + st["dropped_foreign"] >= total:
                break
            await asyncio.sleep(0.02)
        snap = c.netflow_status()                                   # what GET /api/netflow answers
        c.update_settings({"netflow_enabled": False})
        await c.netflow_apply()
        return snap

    st = asyncio.run(run())
    expect = json.loads(json.dumps(REC["expect"]))
    expect["exporters"] = {LOOP[k]: v for k, v in expect["exporters"].items()}
    expect["refused_senders"] = [LOOP[k] for k in expect["refused_senders"]]
    _check(st, expect)
    assert st["enabled"] is True and st["waiting"] == 3 and st["malformed"] == 1
    devs = {x["id"]: x for x in c.paths_summary("24h")["devices"]}
    for dev, want in REC["expect"]["devices"].items():
        assert (devs[dev]["tx"], devs[dev]["rx"]) == (want["tx"], want["rx"]), dev
