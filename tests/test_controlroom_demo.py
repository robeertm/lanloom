"""Sample data of the control room (lanloom/demo_controlroom.py): valid, nothing contacted, seeded once."""
import asyncio
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from crhelp import T0
from lanloom import core as core_mod
from lanloom import demo_controlroom as dc
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.firewall import neutral
from lanloom.store import Store


def make(tmp_path):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=True, probe=True, release_feed="", image_repo="",
                          cookie_secure="never", trusted_origins=(), trusted_proxies=())
    return core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))


def test_sample_gateway_state_is_a_valid_neutral_state_with_the_new_lists():
    st = dc.extend_gateway({"devices": [], "networks": []}, T0)
    neutral.validate(st)
    assert [w["role"] for w in st["wans"]] == ["primary", "backup"] and st["wans"][0]["active"] is True and st["wans"][1]["kind"] == "cellular"
    assert st["cellular"][0]["line"] == dc.LINE_RESERVE and st["ids"] == {"enabled": True, "blocked_24h": 214}


def test_sample_addresses_stay_in_the_sample_network():
    text = repr(dc.nut_event(T0, 0)) + repr(dc.PROBE) + dc.UPS_HOST
    assert dc.UPS_HOST.startswith("192.168.50.") and dc.PROBE["tcp"] == ["192.0.2.53:53"]
    assert "example.org" not in repr(dc.PROBE["tcp"]) and text


def test_sample_ups_event_is_read_by_the_book(tmp_path):
    c = make(tmp_path)
    now = time.time()                                                                   # the core books readings with the wall clock
    c.on_event("int:9", 9, dc.nut_event(now, 10.0))
    v = c.controlroom.ups.view(now)[0]
    assert v["state"] == "online" and v["ups"] == "office" and v["runtime_s"] and v["load"]


def test_seeding_writes_a_week_once_and_opens_no_connection(tmp_path, monkeypatch):
    c = make(tmp_path)

    def boom(*a, **k):
        raise AssertionError("the sample network contacted something")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(asyncio, "open_connection", boom)
    dc.seed(c, T0)
    wan = c.controlroom.wan
    h = wan.history(T0)
    assert h["outages_7d"] == 1 and h["uptime_pct_7d"] < 100
    assert wan.failovers_24h(T0) == 1 and wan.confirmed == "up"
    assert len(c.controlroom.ups.episodes(T0 - 86400, T0)) == 1
    ids = {ch["id"] for ch in c.controlroom.backups.chains(T0)}
    assert {"manual:offsite-copy", "manual:photo-archive", "manual:laptop-image", "manual:nightly-sync"} <= ids
    chains = {ch["id"]: ch for ch in c.controlroom.backups.chains(T0)}
    assert chains["manual:nightly-sync"]["status"] == "running" and chains["manual:photo-archive"]["status"] == "late"
    assert chains["manual:laptop-image"]["status"] == "unknown" and chains["manual:offsite-copy"]["status"] == "ok"
    before = c.store.one("SELECT COUNT(*) AS n FROM wan_slots")["n"]
    dc.seed(c, T0 + 100)                                                               # a second start adds nothing
    assert c.store.one("SELECT COUNT(*) AS n FROM wan_slots")["n"] == before and len(c.controlroom.backups.manual()) == 4
    assert asyncio.run(wan.probe_once())["ok"] is True                                  # the synthetic probe, no connection


def test_tick_feeds_probe_and_watch(tmp_path):
    c = make(tmp_path)
    c.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES('nut','Battery backup','{}',1,0)")
    c.live_cfg[1] = {"state": dc.extend_gateway({"devices": [], "networks": []}, T0), "caps": {}, "family": "x", "t": T0}
    now = time.time()
    dc.tick(c, now, 5.0)
    assert c.controlroom.ups.view(now)[0]["state"] == "online"
    assert c.controlroom.wan.probe_state["ok"] is True
