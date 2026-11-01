from lanloom.scan import neighbors, netinfo

ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
garbage line
wlan0\tZZZZ\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
"""


def test_parse_routes():
    routes = netinfo.parse_routes(ROUTES)
    assert len(routes) == 3
    default = routes[0]
    assert default["dest"] == "0.0.0.0" and default["gateway"] == "192.168.1.1" and default["flags"] & 2
    assert routes[1]["dest"] == "192.168.1.0" and routes[1]["mask"] == "255.255.255.0"


def test_host_with_container_bridge():
    addrs = [("lo", "127.0.0.1", "255.0.0.0"), ("eth0", "192.168.1.50", "255.255.255.0"),
             ("docker0", "172.17.0.1", "255.255.0.0")]
    nets = netinfo.build_networks(addrs, netinfo.parse_routes(ROUTES), in_container=True)
    assert nets == [{"cidr": "192.168.1.0/24", "iface": "eth0", "self_ip": "192.168.1.50",
                     "gateway": "192.168.1.1", "private": True, "size": 256, "host_mode": True}]


def test_bridge_container_not_host_mode():
    addrs = [("lo", "127.0.0.1", "255.0.0.0"), ("eth0", "172.17.0.5", "255.255.0.0")]
    routes = netinfo.parse_routes(
        "h\neth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n")
    nets = netinfo.build_networks(addrs, routes, in_container=True)
    assert len(nets) == 1 and nets[0]["host_mode"] is False and nets[0]["gateway"] == "172.17.0.1"
    # the same layout outside a container is a host on a 172.16/12 LAN
    assert netinfo.build_networks(addrs, routes, in_container=False)[0]["host_mode"] is True


def test_only_virtual_interfaces():
    addrs = [("lo", "127.0.0.1", "255.0.0.0"), ("docker0", "172.17.0.1", "255.255.0.0")]
    nets = netinfo.build_networks(addrs, [], in_container=False)
    assert len(nets) == 1 and nets[0]["iface"] == "docker0" and nets[0]["host_mode"] is False


def test_multiple_interfaces_sorted_default_first():
    addrs = [("eth1", "10.0.5.2", "255.255.255.0"), ("eth0", "192.168.1.50", "255.255.255.0"),
             ("eth2", "203.0.113.7", "255.255.255.0")]
    nets = netinfo.build_networks(addrs, netinfo.parse_routes(ROUTES), in_container=False)
    assert [n["iface"] for n in nets] == ["eth0", "eth1", "eth2"]
    assert nets[2]["private"] is False


def test_detect_networks_never_raises():
    assert isinstance(netinfo.detect_networks(), list)


def test_virtual_prefixes():
    for name in ("docker0", "veth12ab", "br-0123", "virbr0", "lo"):
        assert netinfo.is_virtual(name)
    for name in ("eth0", "enp3s0", "wlan0", "bond0"):
        assert not netinfo.is_virtual(name)


ARP = """IP address       HW type     Flags       HW address            Mask     Device
192.168.1.1      0x1         0x2         02:00:00:00:00:01     *        eth0
192.168.1.20     0x1         0x0         00:00:00:00:00:00     *        eth0
192.168.1.30     0x1         0x2         02:00:00:00:00:1E     *        eth0
not-an-ip        0x1         0x2         02:00:00:00:00:02     *        eth0
192.168.1.40     0x1         0xZZ        02:00:00:00:00:03     *        eth0
192.168.1.41     0x1         0x2         nonsense              *        eth0
short line
"""


def test_parse_arp():
    table = neighbors.parse_arp(ARP)
    assert table == {"192.168.1.1": {"mac": "02:00:00:00:00:01", "iface": "eth0"},
                     "192.168.1.30": {"mac": "02:00:00:00:00:1e", "iface": "eth0"}}


def test_read_arp_missing_file(tmp_path):
    assert neighbors.read_arp(str(tmp_path / "nope")) is None
    p = tmp_path / "arp"
    p.write_text(ARP)
    assert len(neighbors.read_arp(str(p))) == 2


def test_nudge_localhost_only():
    import asyncio
    import socket
    from lanloom.scan.ratelimit import TokenBucket

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(1)
    port = rx.getsockname()[1]
    try:
        sent = asyncio.run(neighbors.nudge(["127.0.0.1"] * 3, TokenBucket(1000), port=port))
        assert sent == 3
        data, _ = rx.recvfrom(10)
        assert data == b""
    finally:
        rx.close()
