"""The page of the control room (lanloom/controlroom/room.py): blocks only with a source, stale is unknown, points, admin data."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from crhelp import T0, FakeCore, ent, gw_entry, hub_entry, line
from lanloom import dnsfilter
from lanloom.controlroom import room as room_mod
from lanloom.controlroom.room import ControlRoom


def make():
    core = FakeCore()
    return core, ControlRoom(core, clock=lambda: T0)


def block(snap, bid):
    return next((b for b in snap["blocks"] if b["id"] == bid), None)


def keys(b):
    return [ln["key"] for ln in b["lines"]]


def router(name="Gateway", ip="192.168.50.1", online=True, **stats):
    return {"kind": "router", "name": name, "mgmt_ip": ip, "online": online, "stats": stats}


def equipment_core(*routers, age=20, ids=None, extra=None):
    core, cr = make()
    st = {"equipment": list(routers)}
    if ids is not None:
        st["ids"] = ids
    st.update(extra or {})
    core.configs = [gw_entry(None, age, **st)]
    return core, cr


# ---------------------------------------------------------------------------------------------------- an empty network
def test_an_empty_network_has_no_blocks_and_no_level():
    core, cr = make()
    snap = cr.snapshot(T0)
    assert snap["blocks"] == [] and snap["points"] == [] and snap["level"] == "none" and snap["sources"] == []
    assert snap["backups"]["chains"] == [] and snap["ups"] == [] and snap["nas"] == []


def test_block_helper_levels_and_staleness():
    b = ControlRoom._block("x", [{"key": "a", "vars": {}, "level": "ok"}, {"key": "b", "vars": {}, "level": "warn"}], 10.0, "#/x")
    assert b["level"] == "warn" and b["fresh"] == "fresh" and b["age_s"] == 10.0 and b["link"] == "#/x"
    b = ControlRoom._block("x", [{"key": "a", "vars": {}, "level": "ok"}], 1000.0, "#/x")
    assert b["fresh"] == "stale" and b["level"] == "unknown"                       # an old picture is never "ok"
    b = ControlRoom._block("x", [{"key": "a", "vars": {}, "level": "danger"}], 1000.0, "#/x")
    assert b["level"] == "unknown"
    assert ControlRoom._block("x", [], 5.0, "#/x")["level"] == "unknown"
    assert ControlRoom._block("x", [{"key": "a", "vars": {}, "level": "ok"}], None, "#/x")["fresh"] == "none"


# ---------------------------------------------------------------------------------------------------- gateway and switches
def test_gateway_block_figures_and_levels():
    core, cr = equipment_core(router(cpu=40, mem=86, temp=91, uptime_s=90000), ids={"enabled": True, "blocked_24h": 214})
    b = block(cr.snapshot(T0), "gateway")
    by = {ln["key"]: ln for ln in b["lines"]}
    assert by["cr.gw.cpu"]["level"] == "ok" and by["cr.gw.mem"]["level"] == "warn" and by["cr.gw.temp"]["level"] == "danger"
    assert by["cr.gw.uptime"]["vars"] == {"up_dur": 90000} and by["cr.gw.ids"]["vars"] == {"n": 214}
    assert b["level"] == "danger" and b["link"] == "#/network" and b["age_s"] == 20.0


@pytest.mark.parametrize("metric,value,level", [("cpu", 84, "ok"), ("cpu", 85, "warn"), ("cpu", 95, "danger"), ("mem", 94, "warn"), ("temp", 74, "ok"),
                                                ("temp", 75, "warn"), ("temp", 90, "danger")])
def test_gateway_limits(metric, value, level):
    core, cr = equipment_core(router(**{metric: value}))
    assert block(cr.snapshot(T0), "gateway")["lines"][0]["level"] == level


def test_gateway_offline_and_ids_switched_off():
    core, cr = equipment_core(router(online=False), ids={"enabled": False})
    b = block(cr.snapshot(T0), "gateway")
    assert keys(b) == ["cr.gw.offline", "cr.gw.ids_off"] and b["level"] == "danger"


def test_two_connections_to_the_same_gateway_give_one_set_of_lines():
    core, cr = make()
    def st():
        return {"equipment": [router(cpu=10)], "ids": {"enabled": True, "blocked_24h": 3}}
    core.configs = [gw_entry(None, 10, key="int:1", **st()), gw_entry(None, 30, key="int:2", **st())]
    b = block(cr.snapshot(T0), "gateway")
    assert keys(b) == ["cr.gw.cpu", "cr.gw.ids"] and b["age_s"] == 10.0
    core.configs[1]["state"]["equipment"][0] = router(name="Other", ip="192.168.50.2", cpu=20)
    b = block(cr.snapshot(T0), "gateway")
    assert keys(b).count("cr.gw.cpu") == 2


def test_no_router_no_gateway_block_and_switches_are_counted():
    core, cr = equipment_core({"kind": "switch", "name": "S1", "online": True}, {"kind": "switch", "name": "S2", "online": False},
                              {"kind": "ap", "name": "AP"})
    snap = cr.snapshot(T0)
    assert block(snap, "gateway") is None
    b = block(snap, "switch")
    assert b["lines"][0] == {"key": "cr.sw.count", "vars": {"online": 1, "total": 2}, "level": "warn"} and keys(b) == ["cr.sw.count", "cr.sw.offline"]
    core, cr = equipment_core({"kind": "switch", "name": "S1", "online": False})
    assert block(cr.snapshot(T0), "switch")["lines"][0]["level"] == "danger"
    core, cr = equipment_core({"kind": "switch", "name": "S1"})
    assert block(cr.snapshot(T0), "switch")["level"] == "ok"                      # nothing says it is offline


def test_a_gateway_picture_older_than_fifteen_minutes_is_unknown_not_ok():
    core, cr = equipment_core(router(cpu=10), age=2000)
    b = block(cr.snapshot(T0), "gateway")
    assert b["fresh"] == "stale" and b["level"] == "unknown"


# ---------------------------------------------------------------------------------------------------- internet
def wan_core(wans, probe=False):
    core, cr = make()
    core.configs = [gw_entry(wans, 10)]
    if probe:
        cr.wan.set_probe({"tcp": ["192.0.2.53:53"]})
    for i in range(3):
        cr.wan.step(T0 - 45 + 15 * i)
    return core, cr


def test_wan_block_up_with_a_reserve_line_and_no_probe():
    core, cr = wan_core([line("p", up=True, active=True, role="primary"), line("b", up=True, active=False, role="backup")])
    b = block(cr.snapshot(T0), "wan")
    assert keys(b) == ["cr.wan.state.up", "cr.wan.reserve_idle", "cr.wan.no_probe"] and b["level"] == "ok"
    assert b["lines"][0]["vars"] == {"since_at": T0 - 45}


def test_wan_block_down_flapping_unknown_levels():
    core, cr = wan_core([line("p", up=False, role="primary")])
    for i in range(3):
        cr.wan.step(T0 - 45 + 15 * i)
    cr.wan.confirmed, cr.wan.since = "down", T0 - 10
    assert block(cr.snapshot(T0), "wan")["level"] == "danger"
    cr.wan.flapping_since = T0 - 5
    b = block(cr.snapshot(T0), "wan")
    assert b["lines"][0]["key"] == "cr.wan.state.flapping" and b["level"] == "warn"
    cr.wan.flapping_since = None
    cr.wan.confirmed = None
    b = block(cr.snapshot(T0), "wan")
    assert b["lines"][0]["key"] == "cr.wan.state.unknown" and b["level"] == "unknown"


def test_wan_block_is_missing_without_any_source_and_present_with_a_probe():
    core, cr = make()
    assert block(cr.snapshot(T0), "wan") is None
    cr.wan.set_probe({"tcp": ["192.0.2.53:53"]})
    b = block(cr.snapshot(T0), "wan")
    assert b is not None and b["lines"][0]["key"] == "cr.wan.state.unknown" and b["level"] == "unknown"


def test_wan_block_reserve_use_disagreement_and_switch_count():
    core, cr = wan_core([line("p", up=False, active=False, role="primary", name="Fibre"), line("b", up=True, active=True, role="backup", name="Mobile")],
                        probe=True)
    cr.wan.feed_probe({"ok": True, "ms": 41.0, "dns_ok": None, "tcp_ok": True}, T0)
    cr.wan.step(T0)
    for i in range(4):
        cr.wan._log(T0 - 100 * (i + 1), "failover", "Mobile", "", "")
    b = block(cr.snapshot(T0), "wan")
    by = {ln["key"]: ln for ln in b["lines"]}
    assert "cr.wan.on_reserve" in by and by["cr.wan.on_reserve"]["level"] == "warn"
    assert not any(k.startswith("cr.wan.disagree") for k in by)                     # the reserve line is up: the gateway and the probe agree
    assert by["cr.wan.switches"] == {"key": "cr.wan.switches", "vars": {"n": 4}, "level": "warn"}
    assert by["cr.wan.latency"]["vars"] == {"ms": 41} and "cr.wan.no_probe" not in by and b["level"] == "warn"


def test_wan_block_says_when_the_gateway_and_the_measurement_disagree():
    core, cr = wan_core([line("p", up=False, role="primary")], probe=True)
    cr.wan.feed_probe({"ok": True, "ms": 20.0, "dns_ok": None, "tcp_ok": True}, T0)
    cr.wan.step(T0)
    cr.wan.step(T0 + 1)                                                             # two results in a row bring it back up
    b = block(cr.snapshot(T0 + 1), "wan")
    d = next(ln for ln in b["lines"] if ln["key"].startswith("cr.wan.disagree"))
    assert d == {"key": "cr.wan.disagree.gateway_down_measured_up", "vars": {}, "level": "warn"} and b["level"] == "warn"
    assert b["lines"][0]["key"] == "cr.wan.state.up"                                 # the measurement counts
    cr.wan.set_probe({"tcp": ["192.0.2.53:53"]})
    cr.wan.feed_probe({"ok": False, "ms": None, "dns_ok": None, "tcp_ok": False}, T0 + 2)
    core.configs = [gw_entry([line("p", up=True, role="primary")], 10)]
    cr.wan.step(T0 + 2)
    assert any(ln["key"] == "cr.wan.disagree.gateway_up_measured_down" for ln in block(cr.snapshot(T0 + 2), "wan")["lines"])


def test_wan_switches_up_to_two_are_only_a_note():
    core, cr = wan_core([line("p", up=True, role="primary"), line("b", up=True, role="backup")])
    cr.wan._log(T0 - 10, "failover", "", "", "")
    cr.wan._log(T0 - 20, "failover", "", "", "")
    by = {ln["key"]: ln for ln in block(cr.snapshot(T0), "wan")["lines"]}
    assert by["cr.wan.switches"]["level"] == "ok"


def test_wan_block_carries_the_mobile_router_lines():
    core, cr = wan_core([line("p", up=True, role="primary")])
    core.configs[0]["state"]["cellular"] = [{"id": "c1", "name": "Router", "rsrp": -115, "sinr": -9, "data_used": 90e9, "data_limit": 100e9, "active": True}]
    b = block(cr.snapshot(T0), "wan")
    by = {ln["key"]: ln for ln in b["lines"]}
    assert by["cr.wan.signal"]["level"] == "danger" and by["cr.wan.signal"]["vars"] == {"name": "Router", "grade_key": "cr.grade.bad"}
    assert by["cr.wan.data"]["level"] == "warn" and by["cr.wan.data"]["vars"] == {"name": "Router", "pct": 90}


# ---------------------------------------------------------------------------------------------------- ups, nas, hub, dns, backups
def feed_ups(cr, status, charge=90.0, runtime=1800.0, name="office", t=T0 - 10):
    cr.feed(1, "nut", {"host": "192.168.50.10", "units": [{"name": name, "description": name, "status": status, "charge": charge, "runtime_s": runtime,
                                                         "load": 31.0}]}, t)


def test_ups_block_lines_per_state():
    core, cr = make()
    assert block(cr.snapshot(T0), "ups") is None
    feed_ups(cr, "OL")
    b = block(cr.snapshot(T0), "ups")
    assert b["lines"] == [{"key": "cr.ups.online", "vars": {"name": "office", "charge": 90, "runtime_dur": 1800, "load": 31}, "level": "ok"}]
    feed_ups(cr, "OB", charge=70.0, runtime=900.0)
    b = block(cr.snapshot(T0), "ups")
    assert b["lines"][0]["key"] == "cr.ups.battery" and b["level"] == "warn" and b["lines"][0]["vars"]["runtime_dur"] == 900
    feed_ups(cr, "OB LB", charge=10.0)
    assert block(cr.snapshot(T0), "ups")["level"] == "danger"
    feed_ups(cr, "RB")
    assert keys(block(cr.snapshot(T0), "ups")) == ["cr.ups.replace"]
    feed_ups(cr, "OVER")
    assert keys(block(cr.snapshot(T0), "ups")) == ["cr.ups.overload"]
    feed_ups(cr, "")
    b = block(cr.snapshot(T0), "ups")
    assert keys(b) == ["cr.ups.unknown"] and b["level"] == "unknown"


def test_a_ups_that_went_quiet_is_unknown_in_the_block():
    core, cr = make()
    feed_ups(cr, "OL", t=T0 - 2000)
    b = block(cr.snapshot(T0), "ups")
    assert b["level"] == "unknown" and keys(b) == ["cr.ups.unknown"]


def test_feed_takes_nut_and_snmp_and_forgets_a_connection():
    core, cr = make()
    cr.feed(1, "snmp", {"host": "h", "ups": {"name": "snmp-ups", "status": "OL"}}, T0)
    cr.feed(1, "snmp", {"host": "h"}, T0)                                           # a frame without UPS values: nothing
    cr.feed(2, "mqtt", {"ups": {"name": "x"}}, T0)
    cr.feed(3, "nut", {"host": "h3", "units": [{"name": "n", "status": "OL"}]}, T0)
    assert sorted(cr.ups.units) == ["nut:3:n", "snmp:1:snmp-ups"]
    cr.forget_integration(1)
    assert sorted(cr.ups.units) == ["nut:3:n"]


def test_nas_block_per_card():
    core, cr = make()
    for i, (title) in enumerate(("Box one", "Box two")):
        core.store.upsert_device({"id": "d-%d" % i, "ip": "192.168.50.%d" % (30 + i), "mac": None, "data": {}, "kind": "nas", "confidence": 0.9, "title": title}, None)
    core.agents = SimpleNamespace(live={}, device_of=lambda aid: None, device_detail=lambda d, now: {"hosts": [{"system": {"disks": [{"mount": "/volume1", "total": 1000 * 10 ** 9, "free": 30 * 10 ** 9}]}}]}
                                  if d == "d-0" else None, containers_by_device=lambda now: {})
    core.hosthealth = SimpleNamespace(readings=lambda now: {"d-0": {"agent": {"t": T0 - 10, "values": {"temp": 80.0}}}})
    b = block(cr.snapshot(T0), "nas")
    by = {ln["key"]: ln for ln in b["lines"]}
    assert by["cr.nas.volume"]["vars"] == {"name": "Box one", "volume": "/volume1", "pct": 97} and by["cr.nas.volume"]["level"] == "danger"
    assert by["cr.nas.temp"]["level"] == "warn" and by["cr.nas.no_reading"]["vars"] == {"name": "Box two"} and by["cr.nas.no_reading"]["level"] == "unknown"
    assert "cr.nas.ok" not in by and b["level"] == "danger"


def test_nas_block_says_fine_only_when_everything_is_fine():
    core, cr = make()
    core.store.upsert_device({"id": "d-0", "ip": "192.168.50.30", "mac": None, "data": {}, "kind": "nas", "confidence": 0.9, "title": "Box"}, None)
    core.agents = SimpleNamespace(live={}, device_of=lambda aid: None, device_detail=lambda d, now: {"hosts": [{"system": {"disks": [{"mount": "/volume1", "total": 1000 * 10 ** 9, "free": 600 * 10 ** 9}]}}]},
                                  containers_by_device=lambda now: {})
    core.hosthealth = SimpleNamespace(readings=lambda now: {"d-0": {"agent": {"t": T0 - 10, "values": {"temp": 40.0}}}})
    b = block(cr.snapshot(T0), "nas")
    assert b["lines"][0] == {"key": "cr.nas.ok", "vars": {"n": 1}, "level": "ok"} and b["level"] == "ok"
    assert block(cr.snapshot(T0), "nas")["age_s"] == 10.0


def test_hub_block_connected_waiting_and_old():
    core, cr = make()
    assert block(cr.snapshot(T0), "hub") is None
    core.hub = {1: hub_entry([ent("sensor.a", "d", "A"), ent("sensor.b", "d", "B")], t=T0 - 60), 2: {"snapshot": None, "states": {}}}
    b = block(cr.snapshot(T0), "hub")
    assert {ln["key"] for ln in b["lines"]} == {"cr.hub.connected", "cr.hub.waiting"}
    assert next(ln for ln in b["lines"] if ln["key"] == "cr.hub.connected")["vars"] == {"n": 2} and b["level"] == "unknown"
    core.hub = {1: hub_entry([ent("sensor.a", "d", "A")], t=T0 - 60)}
    assert block(cr.snapshot(T0), "hub")["level"] == "ok"
    core.hub = {1: hub_entry([ent("sensor.a", "d", "A")], t=T0 - 1000)}
    b = block(cr.snapshot(T0), "hub")
    assert b["lines"][0]["level"] == "unknown" and b["level"] == "unknown"


def test_hub_cadence_can_make_the_picture_younger():
    core, cr = make()
    core.hub = {1: hub_entry([ent("sensor.a", "d", "A")], t=T0 - 1000)}
    core.cadence = {"int:1": SimpleNamespace(last=T0 - 5)}
    b = block(cr.snapshot(T0), "hub")
    assert b["age_s"] == 5.0 and b["level"] == "ok"


def test_dns_block(monkeypatch):
    core, cr = make()
    assert block(cr.snapshot(T0), "dns") is None
    monkeypatch.setattr(dnsfilter, "overview", lambda core, now: [{"name": "Filter A", "health": "ok", "queries": 1200, "share": 0.31},
                                                                  {"name": "Filter B", "health": "bad", "queries": None, "share": None}])
    b = block(cr.snapshot(T0), "dns")
    assert b["lines"][0] == {"key": "cr.dns.line", "vars": {"name": "Filter A", "queries": 1200, "share": 31}, "level": "ok"}
    assert b["lines"][1]["level"] == "danger" and b["lines"][1]["vars"]["queries"] == 0 and b["level"] == "danger"
    monkeypatch.setattr(dnsfilter, "overview", lambda core, now: [])
    assert block(cr.snapshot(T0), "dns") is None

    def boom(core, now):
        raise RuntimeError("x")
    monkeypatch.setattr(dnsfilter, "overview", boom)
    assert block(cr.snapshot(T0), "dns") is None


def test_backup_block_lines():
    core, cr = make()
    assert block(cr.snapshot(T0), "backups") is None
    bk = cr.backups
    for cid in "abcde":
        bk.save_chain(cid, {"name": cid.upper(), "expect_hours": 24}, T0)
    bk.report_run("a", True, now=T0 - 60)
    bk.report_run("b", False, now=T0 - 60)
    bk.report_run("c", True, now=T0 - 5 * 86400)
    bk.report_run("d", True, now=T0 - 5 * 86400)
    bk.report_progress("d", 30, now=T0 - 10)
    b = block(cr.snapshot(T0), "backups")
    assert keys(b) == ["cr.bk.failed", "cr.bk.late", "cr.bk.unknown", "cr.bk.running", "cr.bk.ok"]
    assert b["lines"][-1]["vars"] == {"n": 1, "total": 5} and b["level"] == "danger"
    for cid in "bcde":
        bk.delete_chain(cid)
    b = block(cr.snapshot(T0), "backups")
    assert keys(b) == ["cr.bk.ok"] and b["level"] == "ok"


# ---------------------------------------------------------------------------------------------------- points, sources, admin
def test_points_are_the_warn_and_danger_lines_worst_first_with_stable_ids():
    core, cr = equipment_core(router(cpu=90, temp=95), {"kind": "switch", "name": "S", "online": False})
    snap = cr.snapshot(T0)
    assert [p["level"] for p in snap["points"]] == sorted((p["level"] for p in snap["points"]), key=lambda lv: -{"warn": 3, "danger": 4}[lv])
    ids = [p["id"] for p in snap["points"]]
    assert "gateway:cr.gw.temp" in ids and "gateway:cr.gw.cpu" in ids and "switch:cr.sw.count" in ids
    assert snap["points"][0]["level"] == "danger" and snap["points"][0]["key"] == "cr.gw.temp"
    assert ids == [p["id"] for p in cr.snapshot(T0)["points"]]
    assert all(p["level"] in ("warn", "danger") for p in snap["points"])
    assert snap["level"] == "danger"


def test_unknown_lines_are_no_points():
    core, cr = make()
    feed_ups(cr, "")
    snap = cr.snapshot(T0)
    assert snap["points"] == [] and snap["level"] == "unknown"


def test_sources_are_listed_with_their_freshness():
    core, cr = make()
    core.sources = [{"id": "int:1", "name": "Gateway", "kind": "controller", "grade": "ok", "age_s": 5.0},
                    {"id": "host", "name": "source.name.host", "grade": "stale", "age_s": 5000.0},
                    {"id": "scan", "name": "Scanner", "grade": "ok", "age_s": None}]
    s = cr.snapshot(T0)["sources"]
    assert [(x["id"], x["kind"], x["fresh"]) for x in s] == [("int:1", "controller", "fresh"), ("host", "host", "stale"), ("scan", "scan", "none")]


def test_admin_data_of_a_mobile_router_only_for_admins():
    core, cr = make()
    core.configs = [gw_entry([line("p", up=True, role="primary")], 10,
                             cellular=[{"id": "c1", "name": "Router", "rsrp": -90, "cell_id": "0xBEEF", "tac": "4711", "location": "Sample hill"}])]
    plain = cr.snapshot(T0)
    for secret in ("0xBEEF", "4711", "Sample hill"):
        assert secret not in json.dumps(plain)
    adm = cr.snapshot(T0, admin=True)
    assert adm["wan"]["cellular"][0]["admin"]["cell_id"] == "0xBEEF"
    assert cr.snapshot(T0, admin=False)["wan"]["cellular"][0].get("admin") is None


def test_summary_is_cheap_and_complete():
    core, cr = make()
    feed_ups(cr, "OB")
    cr.backups.save_chain("a", {"name": "A", "expect_hours": 24}, T0)
    s = cr.summary(T0)
    assert s == {"wan": "unknown", "ups": ["battery"], "backups": {"ok": 0, "late": 0, "failed": 0, "running": 0, "unknown": 1, "none": 0}}


def test_the_loop_steps_the_watch_and_survives_a_failing_step(monkeypatch):
    core, cr = make()
    monkeypatch.setattr(room_mod, "LOOP_S", 0.01)
    core.configs = [gw_entry([line("p", up=True, role="primary")], 5)]
    calls = {"n": 0}
    real = cr.wan.step

    def flaky(now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(now)
    cr.wan.step = flaky

    async def go():
        task = asyncio.create_task(cr.loop())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if cr.wan.confirmed is not None:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert calls["n"] >= 2 and cr.wan.confirmed == "up"
