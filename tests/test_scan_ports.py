import asyncio
import socket

from lanloom.scan import ports


def test_lists():
    assert 20 <= len(ports.GENTLE_PORTS) <= 30
    assert 180 <= len(ports.DEEP_PORTS) <= 230
    assert set(ports.GENTLE_PORTS) <= set(ports.DEEP_PORTS)
    assert all(0 < p < 65536 for p in ports.DEEP_PORTS)
    assert set(ports.WEB_PORTS) <= set(ports.DEEP_PORTS)
    assert ports.service_name(1883) == "mqtt" and ports.service_name(9100) == "printer-raw"
    assert ports.service_name(4) == "tcp-4"


def test_service_names_neutral():
    for name in ports.DEEP_PORTS.values():
        assert name == name.lower() and " " not in name and len(name) <= 24


def test_probe_open_and_refused():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    open_port = srv.getsockname()[1]
    tmp = socket.socket()
    tmp.bind(("127.0.0.1", 0))
    closed_port = tmp.getsockname()[1]
    tmp.close()
    try:
        state, rtt = asyncio.run(ports.tcp_probe("127.0.0.1", open_port, 1.0))
        assert state == ports.OPEN and rtt is not None and rtt >= 0
        state, rtt = asyncio.run(ports.tcp_probe("127.0.0.1", closed_port, 1.0))
        assert state == ports.REFUSED and rtt is not None
    finally:
        srv.close()
