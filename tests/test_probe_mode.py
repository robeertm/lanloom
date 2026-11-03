"""LANLOOM_PROBE=1 (trial start of an update candidate, often in the host network) never touches the LAN:
no scanner, no presence checks, no live sources, and the API answers only the health check."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from lanloom import __version__, scan, sources
from lanloom import config as config_mod
from lanloom.core import Core
from lanloom.main import create_app
from lanloom.store import Store

HDR = {"X-Lanloom": "1"}


@pytest.fixture()
def no_network(monkeypatch):
    """Every way into the LAN records the attempt instead."""
    touched = []

    def trap(name):
        def fn(*a, **k):
            touched.append(name)
            raise AssertionError("probe touched the network: " + name)
        return fn

    for name in ("detect_networks", "make_plan", "run"):
        monkeypatch.setattr(scan, name, trap("scan." + name))
    for name in ("HostNet", "Hub", "Mqtt", "Snmp"):
        monkeypatch.setattr(sources, name, trap("sources." + name))
    monkeypatch.setattr(Core, "_presence_round", trap("presence"))
    return touched


def probe_cfg(tmp_path):
    from types import SimpleNamespace
    return SimpleNamespace(data_dir=tmp_path, host="127.0.0.1", port=0, update_check=False, demo=False, probe=True,
                           release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())


def test_core_starts_nothing_in_probe_mode(tmp_path, no_network):
    store = Store(":memory:")
    # the probe runs on a copy of the real data: enabled integrations and remembered networks are present
    store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,?)",
                  ("mqtt", "broker", json.dumps({"host": "10.0.0.5", "port": 1883}), time.time()))
    store.set_setting("rescan_cidrs", ["10.0.0.0/24"])
    store.set_setting("rescan_hours", 1)
    core = Core(probe_cfg(tmp_path), store, None)

    async def go():
        await core.start()
        await asyncio.sleep(0.2)
        assert core._source_tasks == {} and core._tasks == [] and core._scan_task is None
        with pytest.raises(ValueError, match="app.err.probe"):
            core.start_scan(["10.0.0.0/24"])
        await core.stop()

    asyncio.run(go())
    assert no_network == []
    store.close()


@pytest.fixture()
def probe_client(tmp_path, monkeypatch, no_network):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_PROBE", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.delenv("LANLOOM_DEMO", raising=False)
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        c.touched = no_network
        yield c


def test_probe_answers_health_with_its_version(probe_client):
    r = probe_client.get("/api/health")
    assert r.status_code == 200 and r.json()["version"] == __version__


def test_probe_refuses_everything_else(probe_client):
    for method, path in (("GET", "/api/state"), ("GET", "/api/networks"), ("POST", "/api/scan"),
                         ("POST", "/api/login"), ("POST", "/api/integrations/test"), ("GET", "/api/update")):
        r = probe_client.request(method, path, headers=HDR, json={"cidrs": ["10.0.0.0/24"]})
        assert r.status_code == 503 and r.json()["error"] == "app.err.probe", (method, path, r.status_code)
    assert probe_client.touched == []
