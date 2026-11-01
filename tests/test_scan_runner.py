"""run() with every network primitive replaced by fakes: no packet leaves the machine."""
import asyncio
import ipaddress
import pathlib
import re

import pytest

from lanloom.scan import banners, make_plan, mdns, neighbors, netinfo, ports, run, runner, ssdp

NET = "192.168.77.0/24"
OPEN = {("192.168.77.1", 53), ("192.168.77.1", 80), ("192.168.77.10", 80), ("192.168.77.11", 631),
        ("192.168.77.12", 49152), ("192.168.77.13", 1883), ("192.168.77.13", 9100)}
ALIVE = {"192.168.77.1", "192.168.77.10", "192.168.77.11", "192.168.77.12", "192.168.77.13", "192.168.77.50"}


@pytest.fixture
def fake_net(monkeypatch):
    calls = {"nudged": [], "probed": [], "bannered": []}
    monkeypatch.setattr(netinfo, "detect_networks", lambda: [
        {"cidr": NET, "iface": "eth0", "self_ip": "192.168.77.50", "gateway": "192.168.77.1",
         "private": True, "size": 256, "host_mode": True},
        {"cidr": "10.9.0.0/24", "iface": "eth1", "self_ip": "10.9.0.2", "gateway": None,
         "private": True, "size": 256, "host_mode": True}])
    state = {"arp": {}}

    def read_arp(path=None):
        return dict(state["arp"])

    async def nudge(addrs, bucket, port=9):
        calls["nudged"].extend(addrs)
        state["arp"] = {ip: {"mac": "02:00:00:00:00:%02x" % int(ip.rsplit(".", 1)[1]), "iface": "eth0"}
                        for ip in ALIVE - {"192.168.77.50"}}
        return len(addrs)

    async def browse(duration, networks=None, iface_ips=(), progress=None, **kw):
        assert iface_ips == ["192.168.77.50"]
        if progress:
            progress(0.5, 1)
        return ({"192.168.77.11": {"services": [{"type": "_ipp._tcp", "name": "Printer", "port": 631, "txt": {}}],
                                   "hostnames": {"printer.local"}},
                 "192.168.77.99": {"services": [], "hostnames": {"phantom.local"}}}, 3)

    async def search(duration, networks=(), iface_ips=(), progress=None, **kw):
        return ({"192.168.77.12": [{"st": "upnp:rootdevice", "server": "X", "friendly_name": "Player",
                                    "model": "MP-100", "manufacturer": "Example Corp", "device_type": None}],
                 "10.1.1.1": [{"st": "x"}]}, 2)

    async def tcp_probe(ip, port, timeout):
        calls["probed"].append((ip, port))
        assert ipaddress.ip_address(ip) in ipaddress.ip_network(NET)
        if (ip, port) in OPEN:
            return ports.OPEN, 0.5
        if ip in ALIVE:
            return ports.REFUSED, 0.3
        return ports.TIMEOUT, None

    async def fetch_banner(ip, port, tls, timeout=2.0):
        calls["bannered"].append((ip, port))
        return {"title": f"T{port}", "server": "S"}

    async def rnames(ips, timeout):
        return {"192.168.77.10": "web-10.lan"}

    monkeypatch.setattr(neighbors, "read_arp", read_arp)
    monkeypatch.setattr(neighbors, "nudge", nudge)
    monkeypatch.setattr(mdns, "browse", browse)
    monkeypatch.setattr(ssdp, "search", search)
    monkeypatch.setattr(ports, "tcp_probe", tcp_probe)
    monkeypatch.setattr(banners, "fetch_banner", fetch_banner)
    monkeypatch.setattr(runner, "_reverse_names", rnames)
    return calls


def _plan(**kw):
    return make_plan([NET], settle_s=0.0, rate=100, **kw)


def test_result_schema_and_content(fake_net):
    events = []
    res = asyncio.run(run(_plan(), progress=events.append))
    assert res["mode"] == "gentle" and res["finished"] >= res["started"]
    assert [n["cidr"] for n in res["networks"]] == [NET]
    hosts = {h["ip"]: h for h in res["hosts"]}
    assert set(hosts) == ALIVE | {"192.168.77.99"}           # mDNS-only host (no ARP entry) is kept
    assert hosts["192.168.77.99"]["sources"] == ["mdns"] and hosts["192.168.77.99"]["hostnames"] == ["phantom.local"]
    for h in res["hosts"]:
        assert set(h) == {"ip", "mac", "hostnames", "sources", "ports", "mdns", "ssdp", "is_gateway",
                          "is_self", "rtt_ms"}
        for p in h["ports"]:
            assert set(p) == {"port", "proto", "service", "banner"} and p["proto"] == "tcp"
    gw = hosts["192.168.77.1"]
    assert gw["is_gateway"] and gw["mac"] and "arp" in gw["sources"] and "tcp" in gw["sources"]
    assert [p["port"] for p in gw["ports"]] == [53, 80]
    assert gw["ports"][1]["banner"] == {"title": "T80", "server": "S"} and gw["ports"][0]["banner"] is None
    assert hosts["192.168.77.50"]["is_self"] and hosts["192.168.77.50"]["mac"] is None
    assert hosts["192.168.77.11"]["mdns"][0]["type"] == "_ipp._tcp"
    assert "printer.local" in hosts["192.168.77.11"]["hostnames"]
    assert hosts["192.168.77.12"]["ssdp"][0]["model"] == "MP-100" and "ssdp" in hosts["192.168.77.12"]["sources"]
    assert hosts["192.168.77.10"]["hostnames"] == ["web-10.lan"]
    assert hosts["192.168.77.13"]["ports"][0]["service"] == "mqtt"
    assert res["stats"]["probes"] > 0 and res["stats"]["duration_s"] >= 0
    # phases in contract order, each with a final event
    order = []
    for e in events:
        assert set(e) == {"phase", "done", "total", "found"}
        if e["phase"] not in order:
            order.append(e["phase"])
    assert order == ["neighbors", "mdns", "ssdp", "ports", "banners"] or \
        order == ["neighbors", "ssdp", "mdns", "ports", "banners"]
    # only in-range addresses touched; self is not nudged; banners only on web ports
    assert "192.168.77.50" not in fake_net["nudged"] and len(fake_net["nudged"]) == 253
    assert set(fake_net["bannered"]) == {("192.168.77.1", 80), ("192.168.77.10", 80), ("192.168.77.12", 49152)}


def test_out_of_scope_answers_dropped(fake_net):
    res = asyncio.run(run(_plan()))
    ips = [h["ip"] for h in res["hosts"]]
    assert "10.1.1.1" not in ips and all(ip.startswith("192.168.77.") for ip in ips)


def test_switches_disable_phases(fake_net):
    res = asyncio.run(run(_plan(mdns=False, ssdp=False, banners=False, rdns=False)))
    assert not fake_net["bannered"]
    assert all(not h["mdns"] and not h["ssdp"] for h in res["hosts"])


def test_broken_progress_and_phase_errors_contained(fake_net, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("mdns exploded")
    monkeypatch.setattr(mdns, "browse", boom)

    def bad_progress(ev):
        raise ValueError("ui gone")

    res = asyncio.run(run(_plan(), progress=bad_progress))
    assert len(res["hosts"]) >= 5


def test_async_progress_callback(fake_net):
    seen = []

    async def cb(ev):
        seen.append(ev["phase"])

    async def go():
        res = await run(_plan(), progress=cb)
        await asyncio.sleep(0)
        return res

    asyncio.run(go())
    assert "ports" in seen


def test_cancellation(fake_net, monkeypatch):
    async def slow_probe(ip, port, timeout):
        await asyncio.sleep(5)
        return ports.TIMEOUT, None
    monkeypatch.setattr(ports, "tcp_probe", slow_probe)

    async def go():
        task = asyncio.create_task(run(_plan(mdns=False, ssdp=False)))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(go(), 3))


def test_run_revalidates_plan(fake_net):
    plan = _plan()
    plan["cidrs"] = ["8.8.8.0/24"]
    with pytest.raises(ValueError):
        asyncio.run(run(plan))
    plan = _plan()
    plan["mode"] = "deep"
    with pytest.raises(ValueError):
        asyncio.run(run(plan))


def test_routed_range_uses_tcp_sweep(fake_net, monkeypatch):
    monkeypatch.setattr(netinfo, "detect_networks", lambda: [])
    res = asyncio.run(run(make_plan(["192.168.77.0/28", "192.168.77.48/29"], settle_s=0.0, rate=100,
                                    mdns=False, ssdp=False)))
    assert not fake_net["nudged"]
    found = {h["ip"] for h in res["hosts"]}
    assert ALIVE <= found
    assert res["networks"][0]["iface"] is None and len(res["networks"]) == 2


# ---------------------------------------------------------------- static hygiene of the package
PKG = pathlib.Path(runner.__file__).parent


def test_no_raw_sockets_or_privileged_calls():
    src = "\n".join(p.read_text() for p in PKG.glob("*.py"))
    for forbidden in ("SOCK_RAW", "IPPROTO_ICMP", "AF_PACKET", "subprocess", "os.system", "shell=True"):
        assert forbidden not in src


def test_runtime_identifiers_marked():
    for p in PKG.glob("*.py"):
        for line in p.read_text().splitlines():
            if re.search(r"docker|containerenv", line, re.IGNORECASE):
                assert "identifier:" in line, f"{p.name}: {line.strip()}"


def test_english_only():
    for p in PKG.glob("*.py"):
        assert not re.search(r"[äöüÄÖÜß]", p.read_text()), p.name
