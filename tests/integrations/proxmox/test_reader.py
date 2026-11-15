"""The read half against a local fake of the host API: what is sent, what is refused, token checks, errors."""
import asyncio
import logging
import re

import pytest

from lanloom.firewall import neutral
from lanloom.integrations.proxmox import read
from lanloom.integrations.transport import Forbidden
from lanloom.sources._common import SourceError

from . import fixtures as fx
from .fake_pve import SECRET, TOKEN_ID, FakePve


@pytest.fixture()
def pve():
    srv = FakePve()
    yield srv
    srv.stop()


def reader(srv, **kw):
    return read.ProxmoxReader(url=srv.url, token_id=kw.pop("token_id", TOKEN_ID), token_secret=kw.pop("token_secret", SECRET), **kw)


@pytest.mark.parametrize("kw", [{"url": ""}, {"url": "ftp://x"}, {"url": "https://user:pw@x"}, {"token_id": "lanloom"},
                                {"token_id": "lanloom@pve"}, {"token_id": "a b@pve!x"}, {"token_secret": ""}, {"token_secret": "short"},
                                {"token_secret": "has space inside it"}])
def test_bad_settings_are_refused(kw):
    args = {"url": "https://192.0.2.1:8006", "token_id": TOKEN_ID, "token_secret": SECRET}
    args.update(kw)
    with pytest.raises(SourceError) as e:
        read.ProxmoxReader(**args)
    assert e.value.key == "source.family.config"


def test_port_defaults_to_8006():
    r = read.ProxmoxReader(url="https://192.0.2.1", token_id=TOKEN_ID, token_secret=SECRET)
    assert r._base == "https://192.0.2.1:8006"
    assert read.ProxmoxReader(url="https://192.0.2.1:443/", token_id=TOKEN_ID, token_secret=SECRET)._base == "https://192.0.2.1:443"


def test_full_poll_reads_every_area_and_gives_the_fixture_state(pve):
    r = reader(pve)
    raw = r.collect(False)
    ev = r.config_event(raw, fx.NOW)
    st = ev["state"]
    neutral.validate(st)
    assert len(st["hosts"]) == 2 and len(st["guests"]) == 5 and len(st["bridges"]) == 7 and len(st["storage"]) == 4
    assert len(st["fw_rules"]) == 4 and len(st["guest_rules"]) == 3 and len(st["sdn_vnets"]) == 2 and len(st["backup_jobs"]) == 2
    web = next(g for g in st["guests"] if g["vmid"] == 100)
    assert web["ips"] == ["192.168.50.80"] and web["backup"]["verified"] and web["rrd"]
    assert ev["capabilities"]["read"] == ["inventory", "topology", "guests", "bridges", "networks", "firewall", "storage", "backups", "stats"]
    assert ev["capabilities"]["api"]["version"] == "8.2.4" and ev["capabilities"]["write"] == {}


def test_only_get_is_sent_and_every_path_is_on_the_allow_list(pve):
    r = reader(pve)
    r.collect(False)
    r.collect(True)
    r.scope()
    assert pve.methods() == {"GET"}
    for p in pve.paths():
        assert read.ALLOW.check("GET", p), p
    assert pve.count(r"/agent/network-get-interfaces") == 1                    # only the running guest with an agent
    assert pve.count(r"/nodes/node-a/storage/backup-share/content") + pve.count(r"/nodes/node-b/storage/backup-share/content") == 1
    assert pve.count(r"/qemu/9000/firewall") == 0 and pve.count(r"/qemu/9000/config") == 1   # a template: config only


def test_light_poll_reads_only_the_cluster_lists(pve):
    r = reader(pve)
    r.collect(False)
    n = len(pve.requests)
    r.collect(True)
    assert sorted(p for p in pve.paths()[n:]) == ["/api2/json/cluster/resources", "/api2/json/cluster/status", "/api2/json/version"]


def test_traffic_rates_from_counter_deltas(pve, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(read.time, "monotonic", lambda: clock[0])
    r = reader(pve)
    assert r.collect(True)["traffic"] == {}
    for res in pve.resources:
        if res.get("id") == "qemu/100":
            res["netin"] += 60_000
            res["netout"] += 120_000
        if res.get("id") == "qemu/101":
            res["netin"] = 5                                     # counter restarted: no rate
    clock[0] += 60
    assert r.collect(True)["traffic"] == {"qemu/100": [1000, 2000], "lxc/200": [0, 0]}


def test_anything_outside_the_allow_list_is_refused_before_a_byte_is_sent(pve):
    r = reader(pve)
    for method, path in (("POST", "/api2/json/nodes/node-a/qemu/100/status/start"), ("PUT", "/api2/json/nodes/node-a/qemu/100/config"),
                         ("DELETE", "/api2/json/nodes/node-a/qemu/100"), ("GET", "/api2/json/nodes/node-a/execute"),
                         ("GET", "/api2/json/access/ticket"), ("GET", "/api2/json/nodes/node-a/qemu/100/vncproxy"),
                         ("GET", "/api2/json/nodes/../qemu/100/config"), ("GET", "/api2/json/cluster/resources?type=vm&x=1")):
        with pytest.raises(Forbidden):
            r._t.request(method, path)
    assert pve.requests == [] and r._t.refused == 8


def test_allow_list_is_read_only():
    assert not read.ALLOW.write
    assert {rule.method for rule in read.ALLOW.rules} == {"GET"}


def test_token_travels_in_the_header_only(pve):
    r = reader(pve)
    r.collect(False)
    for method, path, heads in pve.requests:
        assert heads["authorization"] == "PVEAPIToken=%s=%s" % (TOKEN_ID, SECRET)
        assert SECRET not in path and "cookie" not in heads


def test_refused_token(pve):
    r = reader(pve, token_secret="ffffffff-0000-0000-0000-000000000000")
    with pytest.raises(SourceError) as e:
        r.collect(False)
    assert e.value.key == "source.family.token_refused"


def test_area_the_token_may_not_read_is_narrowed_and_the_rest_read(pve):
    pve.fail[r"/firewall/"] = 403
    r = reader(pve)
    ev = r.config_event(r.collect(False), fx.NOW)
    assert "firewall" not in ev["capabilities"]["read"] and "guests" in ev["capabilities"]["read"]
    assert ev["state"]["fw_rules"] == [] and len(ev["state"]["guests"]) == 5
    del pve.fail[r"/firewall/"]
    assert "firewall" in r.config_event(r.collect(False), fx.NOW)["capabilities"]["read"]     # asked again on the next full poll


def test_403_on_the_resource_list_narrows_inventory(pve):
    pve.fail[r"/cluster/resources$"] = 403
    r = reader(pve)
    ev = r.config_event(r.collect(False), fx.NOW)
    assert "inventory" not in ev["capabilities"]["read"]


def test_missing_features_and_dead_nodes_are_skipped(pve):
    pve.fail[r"/cluster/sdn/"] = 501
    pve.fail[r"/nodes/node-b/"] = 595
    pve.fail[r"/rrddata"] = 500
    r = reader(pve)
    st = r.config_event(r.collect(False), fx.NOW)["state"]
    assert st["sdn_vnets"] == [] and len(st["hosts"]) == 2
    assert all(g["rrd"] is None for g in st["guests"])
    assert "networks" not in r.capabilities()["read"]


def test_unexpected_answers_become_reasons_not_crashes(pve):
    pve.fail[r"/version"] = 502
    with pytest.raises(SourceError) as e:
        reader(pve).collect(False)
    assert e.value.key == "source.family.protocol"
    dead = read.ProxmoxReader(url="http://127.0.0.1:9", token_id=TOKEN_ID, token_secret=SECRET, timeout=1)
    with pytest.raises(SourceError) as e:
        dead.collect(True)
    assert e.value.key == "source.family.unreachable"


def test_request_budget_keeps_what_was_read(pve, monkeypatch):
    monkeypatch.setattr(read, "MAX_REQUESTS", 12)
    r = reader(pve)
    st = r.config_event(r.collect(False), fx.NOW)["state"]
    assert len(st["hosts"]) == 2 and len(pve.requests) <= 3 + 12
    neutral.validate(st)


@pytest.mark.parametrize("perms,scope", [(fx.PERMISSIONS_READ, "read_only"), (fx.PERMISSIONS_WRITE, "can_write"),
                                         ({"/": {"Sys.Audit": 1, "Sys.Syslog": 1, "VM.GuestAgent.Audit": 1}}, "read_only"),
                                         ({"/": {"Sys.Audit": 1, "VM.Monitor": 1}}, "can_write"),
                                         ({"/": {"Sys.Audit": 1, "Sys.Modify": 0}}, "read_only"), ({}, "unknown")])
def test_token_rights(perms, scope):
    srv = FakePve(permissions=perms)
    try:
        r = reader(srv)
        assert r.scope() == scope
        assert asyncio.run(r.account_scope()) == scope
    finally:
        srv.stop()


def test_superuser_token_counts_as_writing_without_asking(pve):
    r = reader(pve, token_id="root@pam!lanloom")
    assert r.scope() == "can_write" and pve.requests == []


def test_secret_never_shows_up_in_repr_str_errors_or_logs(pve, caplog):
    caplog.set_level(logging.DEBUG)
    r = reader(pve)
    assert SECRET not in repr(r) and SECRET not in str(r) and SECRET not in repr(r._t) and SECRET not in repr(vars(r))
    pve.fail[r"/version"] = 401
    with pytest.raises(SourceError) as e:
        r.collect(False)
    assert SECRET not in str(e.value) and SECRET not in repr(e.value)
    assert SECRET not in caplog.text


def test_test_returns_counts_and_poll_events(pve):
    r = reader(pve)
    ok, key, details = asyncio.run(r.test())
    assert ok and key == "source.family.ok"
    assert details == {"version": "8.2.4", "nodes": 2, "guests": 5, "bridges": 7, "storage": 4, "rules": 7}
    evs = asyncio.run(r.poll(light=True))
    assert [e["type"] for e in evs] == ["topology", "config"] and evs[0]["source"] == "hypervisor"
    assert evs[1]["family"] == "proxmox" and len(evs[1]["state"]["guests"]) == 5     # light poll keeps the cached full read


def test_run_emits_status_topology_and_config(pve, monkeypatch):
    events = []

    async def go():
        r = reader(pve, interval=5)
        task = asyncio.create_task(r.run(events.append))
        for _ in range(200):
            await asyncio.sleep(0.02)
            if any(e.get("type") == "status" and e.get("ok") for e in events):
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    types = [e["type"] for e in events]
    assert "topology" in types and "config" in types
    assert any(e["type"] == "status" and e["ok"] and e["key"] == "source.family.ok" for e in events)


def test_paths_of_the_allow_list_are_anchored():
    for rule in read.ALLOW.rules:
        assert rule.path.pattern.startswith(r"\A(?:/api2/json") and rule.path.pattern.endswith(r")\Z")
    assert not re.search(r"\.\*", "".join(read.GET_PATHS))
