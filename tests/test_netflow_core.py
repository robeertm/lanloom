"""NetFlow receiver wired into the core: settings rules, status route, data paths source, real UDP booking."""
import asyncio
import ipaddress
import socket
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod, netflow
from lanloom.main import create_app
from lanloom.store import Store

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        c.anon = TestClient(app, base_url="http://lanloom.test")
        assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
        yield c


def _put(client, body):
    return client.put("/api/settings", json=body, headers=HDR)


def test_enabling_without_an_exporter_is_refused(client):
    assert _put(client, {"netflow_enabled": False, "netflow_exporters": []}).status_code == 200
    r = _put(client, {"netflow_enabled": True})
    assert r.status_code == 400 and r.json()["error"] == "settings.netflow.exporter_required"
    r = _put(client, {"netflow_enabled": True, "netflow_exporters": []})
    assert r.status_code == 400 and r.json()["error"] == "settings.netflow.exporter_required"
    assert client.get("/api/settings").json()["netflow_enabled"] is False
    # removing the last exporter while the receiver is on is refused as well
    assert _put(client, {"netflow_enabled": True, "netflow_exporters": ["192.0.2.1"]}).status_code == 200
    r = _put(client, {"netflow_exporters": []})
    assert r.status_code == 400 and r.json()["error"] == "settings.netflow.exporter_required"
    assert client.get("/api/settings").json()["netflow_exporters"] == ["192.0.2.1"]


@pytest.mark.parametrize("patch, key", [
    ({"netflow_exporters": ["192.0.2.0/24"]}, "settings.netflow.invalid_exporter"),
    ({"netflow_exporters": ["0.0.0.0"]}, "settings.netflow.invalid_exporter"),
    ({"netflow_exporters": ["router"]}, "settings.netflow.invalid_exporter"),
    ({"netflow_port": 80}, "settings.netflow.invalid_port"),
    ({"netflow_port": 70000}, "settings.netflow.invalid_port"),
    ({"netflow_lan_networks": ["2001:db8::/129"]}, "settings.paths.invalid_network"),
])
def test_invalid_values_change_nothing(client, patch, key):
    before = client.get("/api/settings").json()
    r = _put(client, dict(patch, home_country="FR"))
    assert r.status_code == 400 and r.json()["error"] == key
    after = client.get("/api/settings").json()
    assert after == before


def test_valid_settings_and_status(client):
    r = _put(client, {"netflow_enabled": True, "netflow_port": 9995, "netflow_exporters": ["::ffff:192.0.2.1", "2001:db8::1"],
                      "netflow_lan_networks": ["2001:db8:1::/48"]})
    assert r.status_code == 200
    s = r.json()
    assert s["netflow_exporters"] == ["192.0.2.1", "2001:db8::1"] and s["netflow_port"] == 9995
    st = client.get("/api/netflow").json()
    assert st["enabled"] is True and st["port"] == 9995 and set(st["exporters"]) == {"192.0.2.1", "2001:db8::1"}
    assert client.anon.get("/api/netflow").status_code == 401
    # per exporter (W58): what the settings card draws, and the senders that are not allowed with their protocol
    keys = {"version", "packets", "flows", "booked", "bytes", "first", "last", "templates", "expired", "restarts", "waiting", "malformed"}
    for ex in st["exporters"].values():
        assert keys <= set(ex), sorted(keys - set(ex))
    assert all({"ip", "packets", "last", "version"} <= set(r) for r in st["refused_senders"])


def test_demo_names_netflow_as_a_source(client):
    for _ in range(50):
        d = client.get("/api/paths").json()
        if d["geo"]["loaded"]:
            break
        time.sleep(0.1)
    assert d["source"]["kind"] == "both" and d["source"]["netflow"]["exporters"] == ["192.168.50.1"]
    assert _put(client, {"netflow_enabled": False}).status_code == 200
    assert client.get("/api/paths").json()["source"]["kind"] == "host"


def _v5(records):
    head = struct.pack("!HHIIIIBBH", 5, len(records), 1000, int(time.time()), 0, 1, 0, 0, 0)
    body = b"".join(struct.pack("!4s4s4sHHIIIIHHBBBBHHBBH", ipaddress.ip_address(s).packed, ipaddress.ip_address(d).packed,
                                b"\0\0\0\0", 0, 0, 1, n, 0, 0, 40000, 443, 0, 0, 6, 0, 0, 0, 0, 0, 0) for s, d, n in records)
    return head + body


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_real_receiver_books_lan_devices_and_skips_this_host(tmp_path):
    from lanloom.core import Core
    c = Core(SimpleNamespace(data_dir=tmp_path, demo=False, probe=False), Store(":memory:"), None)
    c.geo.start(background=False)
    c.ip_to_device = {"192.0.2.10": "dev-a"}
    c.self_ip = "192.0.2.11"
    c._nf_lan = (time.time(), [ipaddress.ip_network("192.0.2.0/24")])
    port = _free_udp_port()
    c.update_settings({"netflow_enabled": True, "netflow_port": port, "netflow_exporters": ["127.0.0.1"]})

    async def run():
        await c.netflow_apply()
        assert c.netflow.stats()["listening"], c.netflow.stats()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.sendto(_v5([("192.0.2.10", "8.8.8.8", 5000), ("8.8.8.8", "192.0.2.20", 7000), ("192.0.2.11", "8.8.8.8", 900)]),
                  ("127.0.0.1", port))
        tx.close()
        for _ in range(100):
            if c.netflow.stats()["flows"]:
                break
            await asyncio.sleep(0.02)
        c.update_settings({"netflow_enabled": False})
        await c.netflow_apply()
        assert not c.netflow.stats()["listening"]

    asyncio.run(run())
    st = c.netflow.stats()
    assert st["booked"] == 2 and st["skipped_self"] == 1
    d = c.paths_summary("24h")
    devs = {x["id"]: x for x in d["devices"]}
    assert devs["dev-a"]["tx"] == 5000 and devs[netflow.UNKNOWN_DEVICE]["rx"] == 7000
    assert d["source"]["kind"] == "host"


def test_probe_start_never_opens_the_port(tmp_path):
    from lanloom.core import Core
    c = Core(SimpleNamespace(data_dir=tmp_path, demo=False, probe=True), Store(":memory:"), None)
    c.update_settings({"netflow_enabled": True, "netflow_port": _free_udp_port(), "netflow_exporters": ["127.0.0.1"]})
    asyncio.run(c.netflow_apply())
    assert not c.netflow.stats()["listening"]


def test_ipv6_addresses_of_devices_are_known(tmp_path):
    from lanloom.core import Core
    c = Core(SimpleNamespace(data_dir=tmp_path, demo=False, probe=True), Store(":memory:"), None)
    c.ip6_to_device = {"2001:db8:1::10": "dev-b"}
    assert c._netflow_device("2001:db8:1::10") == "dev-b" and c._netflow_device("2001:db8:1::11") is None
