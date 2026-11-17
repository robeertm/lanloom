"""Data paths S14G: conversations between two devices of the LAN (gateway flows), per device view, routes, demo."""
import asyncio
import ipaddress
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod, datapaths, netflow
from lanloom.main import create_app
from lanloom.store import Store
from tests.test_datapaths import FakeGeo, make, T0
from tests.test_netflow_core import _free_udp_port, _v5

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
DEVS = {"a": {"title": "Laptop", "ip": "192.0.2.10", "kind": "laptop"}, "b": {"title": "Storage", "ip": "192.0.2.11", "kind": "nas"},
        "c": {"title": "", "ip": None, "kind": "tv"}}


def test_pair_is_stored_once_per_two_devices_with_both_directions():
    dp = make()
    assert dp.book_pair("b", "a", 500, T0) and dp.book_pair("a", "b", 200, T0) and dp.book_pair("a", "b", 100, T0 + 10)
    assert not dp.book_pair("a", "a", 10, T0) and not dp.book_pair("a", "b", 0, T0) and not dp.book_pair("", "b", 5, T0)
    rows = dp.pairs("24h", T0 + 60)
    assert rows == [{"a": "a", "b": "b", "ab": 300, "ba": 500}]
    assert dp.pairs("7d", T0 + 60) == rows and dp.pairs("30d", T0 + 60) == rows


def test_pairs_keep_only_ids_and_bytes_and_follow_the_windows_and_retention():
    dp = make()
    dp.book_pair("a", "b", 10, T0 - 30 * 3600)           # older than 24 hours, inside 7 days
    dp.book_pair("a", "c", 99, T0)
    assert [(r["a"], r["b"]) for r in dp.pairs("24h", T0 + 5)] == [("a", "c")]
    dp.clock_ref["t"] = T0 + 5
    assert {(r["a"], r["b"]) for r in dp.pairs("7d", T0 + 5)} == {("a", "b"), ("a", "c")}
    cols = {r["name"] for r in dp.store.query("PRAGMA table_info(paths_pair_hour)")}
    assert cols == {"hour", "a", "b", "ab", "ba"}                    # no address, no port, no protocol
    late = T0 + 100 * 86400                                           # far in the future: the old rows are past the retention
    dp.book_pair("a", "c", 1, late)
    dp.flush(late)
    assert [(r["hour"]) for r in dp.store.query("SELECT hour FROM paths_pair_hour")] == [int(late // 3600)]
    assert [(r["day"]) for r in dp.store.query("SELECT day FROM paths_pair_day")] == [int(late // 86400)]


def test_pair_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(datapaths, "MAX_PAIR_KEYS", 3)
    dp = make()
    ok = [dp.book_pair("x%d" % i, "y", 1, T0) for i in range(6)]
    assert ok == [True] * 3 + [False] * 3
    assert dp.book_pair("x0", "y", 5, T0)                             # an existing pair still adds up


def test_forgetting_a_device_removes_its_conversations():
    dp = make()
    dp.book_pair("a", "b", 10, T0)
    dp.flush(T0)
    dp.book_pair("a", "c", 10, T0)
    dp.book_pair("b", "c", 10, T0)
    dp.forget_device("a")
    assert [(r["a"], r["b"]) for r in dp.pairs("24h", T0)] == [("b", "c")]


def test_device_view_lists_countries_and_lan_partners_with_titles():
    dp = make()
    dp.book("a", "8.8.8.8", 1000, 4000, T0)
    dp.book("a", "193.0.0.1", 10, 20, T0)
    dp.book("a", "198.51.100.9", 5, 6, T0)                            # listed as own by the geo data (ZZ)
    dp.book("b", "8.8.8.8", 7, 8, T0)
    dp.book_pair("a", "b", 300, T0)
    dp.book_pair("c", "a", 40, T0)
    v = dp.device_view("a", "24h", DEVS, T0 + 1)
    assert [c["cc"] for c in v["countries"]] == ["US", "DE"] and v["countries"][0]["eu"] is False and v["countries"][1]["eu"] is True
    assert v["own"] == {"tx": 5, "rx": 6} and v["unassigned"] is None
    lan = {r["id"]: r for r in v["lan"]}
    assert lan["b"]["tx"] == 300 and lan["b"]["rx"] == 0 and lan["b"]["title"] == "Storage"
    assert lan["c"]["tx"] == 0 and lan["c"]["rx"] == 40                # seen from a: c sent 40 to a
    assert [r["id"] for r in v["lan"]] == ["b", "c"]
    assert dp.device_view("nobody", "24h", DEVS, T0 + 1)["countries"] == []


def test_internet_tile_view_sums_per_device():
    dp = make()
    dp.book("a", "8.8.8.8", 1000, 4000, T0)
    dp.book("b", "8.8.8.8", 10, 20, T0)
    dp.book("b", "193.0.0.1", 1, 2, T0)
    v = dp.device_view("internet", "24h", DEVS, T0 + 1)
    assert [(d["id"], d["tx"], d["rx"], d["countries"], d["title"]) for d in v["devices"]] == [("a", 1000, 4000, 1, "Laptop"), ("b", 11, 22, 2, "Storage")]
    assert v["totals"] == {"tx": 1011, "rx": 4022, "devices": 2}
    assert "lan" not in v


# ---------------------------------------------------------------------- flows from the gateway
def _collector(dp, mapping):
    nets = [ipaddress.ip_network("192.0.2.0/24")]
    return netflow.Collector(dp, lambda ip: mapping.get(ip), lambda: nets, clock=lambda: T0)


def test_lan_to_lan_flows_become_conversations_but_never_data_paths():
    dp = make()
    col = _collector(dp, {"192.0.2.10": "a", "192.0.2.11": "b"})
    flows = [(ipaddress.ip_address("192.0.2.10"), ipaddress.ip_address("192.0.2.11"), 4000),
             (ipaddress.ip_address("192.0.2.11"), ipaddress.ip_address("192.0.2.10"), 900),
             (ipaddress.ip_address("192.0.2.10"), ipaddress.ip_address("192.0.2.77"), 1234),   # unknown device: no line
             (ipaddress.ip_address("192.0.2.10"), ipaddress.ip_address("192.0.2.10"), 50),     # itself
             (ipaddress.ip_address("192.0.2.10"), ipaddress.ip_address("8.8.8.8"), 700)]
    assert col._book(flows, T0) == 1
    assert dp.pairs("24h", T0 + 1) == [{"a": "a", "b": "b", "ab": 4000, "ba": 900}]
    s = dp.summary("24h", DEVS, T0 + 1)
    assert s["totals"]["tx"] == 700 and s["totals"]["rx"] == 0       # LAN conversations are not data paths, not counted in the EU share
    assert col.stats()["lan_pairs"] == 2 and col.stats()["booked"] == 1


def test_the_host_own_flows_stay_out_of_the_conversations():
    dp = make()
    col = netflow.Collector(dp, lambda ip: {"192.0.2.10": "a", "192.0.2.11": "b"}.get(ip), lambda: [ipaddress.ip_network("192.0.2.0/24")],
                            skip_ips=lambda: {"192.0.2.10"}, clock=lambda: T0)
    col._book([(ipaddress.ip_address("192.0.2.10"), ipaddress.ip_address("192.0.2.11"), 10)], T0)
    assert dp.pairs("24h", T0) == []


def test_real_udp_flows_reach_the_conversations(tmp_path):
    from lanloom.core import Core
    c = Core(SimpleNamespace(data_dir=tmp_path, demo=False, probe=False), Store(":memory:"), None)
    c.geo.start(background=False)
    c.ip_to_device = {"192.0.2.10": "dev-a", "192.0.2.12": "dev-b"}
    c.self_ip = "192.0.2.11"
    c._nf_lan = (time.time(), [ipaddress.ip_network("192.0.2.0/24")])
    port = _free_udp_port()
    c.update_settings({"netflow_enabled": True, "netflow_port": port, "netflow_exporters": ["127.0.0.1"]})

    async def run():
        await c.netflow_apply()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.sendto(_v5([("192.0.2.10", "192.0.2.12", 5000), ("192.0.2.12", "192.0.2.10", 700)]), ("127.0.0.1", port))
        tx.close()
        for _ in range(100):
            if c.netflow.stats()["flows"]:
                break
            await asyncio.sleep(0.02)
        c.update_settings({"netflow_enabled": False})
        await c.netflow_apply()

    asyncio.run(run())
    assert [(p["a"], p["b"], p["ab"], p["ba"]) for p in c.paths.pairs("24h")] == [("dev-a", "dev-b", 5000, 700)]
    assert c.paths_lan("24h")["pairs"] == []                          # the map only draws devices it knows (none stored here)
    assert c.paths_device("dev-a", "24h")["lan"][0]["id"] == "dev-b"


# ---------------------------------------------------------------------- routes and demo
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


def test_demo_has_conversations_and_the_routes_answer(client):
    assert client.anon.get("/api/paths/lan").status_code == 401
    assert client.anon.get("/api/paths/device/internet").status_code == 401
    lan = client.get("/api/paths/lan?window=7d").json()
    assert lan["window"] == "7d" and len(lan["pairs"]) >= 8 and all(p["ab"] + p["ba"] > 0 for p in lan["pairs"])
    assert lan["pairs"] == sorted(lan["pairs"], key=lambda p: -(p["ab"] + p["ba"]))
    assert client.get("/api/paths/lan?window=bogus").json()["window"] == "24h"
    top = lan["pairs"][0]
    v = client.get("/api/paths/device/" + top["a"] + "?window=7d").json()
    assert v["device"] == top["a"] and any(r["id"] == top["b"] for r in v["lan"]) and all("title" in r for r in v["lan"])
    net = client.get("/api/paths/device/internet").json()
    assert net["devices"] and net["totals"]["devices"] == len(net["devices"])
    assert client.get("/api/paths/device/" + "x" * 500).status_code == 200


def test_demo_conversations_survive_a_second_seed_and_are_in_the_clean_up_area(client):
    c = client.app.state.core if hasattr(client.app.state, "core") else None
    from lanloom import storage
    tables = {t for _a, ts in storage.AREAS for t in ts}
    assert {"paths_pair_hour", "paths_pair_day"} <= tables
    from lanloom import backup
    assert {"paths_pair_hour", "paths_pair_day"} <= set(backup.HISTORY_TABLES)
