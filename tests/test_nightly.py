"""Nightly report: schedule with a fixed clock, catch-up, empty sources, journal watcher, sections, tick marks, delivery.

Every test uses a fixed clock (never the wall clock): reports refer to "last night" and would flip around midnight.
Names and addresses are sample data (192.168.50.0/24, RFC 5737 outside).
"""
import asyncio
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from lanloom import core as core_mod
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.nightly import Reports, contrib, deliver, render
from lanloom.nightly.journal import GONE_AFTER_S, START_GRACE_S, intervals
from lanloom.store import Store

TZ = "Europe/Amsterdam"
Z = ZoneInfo(TZ)


def at(y, mo, d, hh=0, mm=0, ss=0) -> float:
    return datetime(y, mo, d, hh, mm, ss, tzinfo=Z).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, clock, demo=False):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=demo, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    vault = Vault(store, b"k" * KEY_BYTES)
    c = core_mod.Core(cfg, store, vault)
    rep = Reports(c, Path(tmp_path), clock=clock)
    rep.cert_check = lambda: []
    c.reports = rep
    store.set_setting("report_cfg", {"time": "06:30", "tz": TZ, "keep_days": 30, "link": ""})
    return c, rep


def run(coro):
    return asyncio.run(coro)


def add_device(c, n, title, kind="computer", hidden=0):
    dev_id = "d-test-%d" % n
    c.store.upsert_device({"id": dev_id, "ip": "192.168.50.%d" % n, "mac": None, "data": {}, "kind": kind, "confidence": 0.9,
                           "title": title}, None)
    if hidden:
        c.store.execute("UPDATE devices SET hidden=1 WHERE id=?", (dev_id,))
    return dev_id


def add_integration(c, kind, name, cfg=None):
    return c.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES(?,?,?,1,0)",
                           (kind, name, json.dumps(cfg or {}))).lastrowid


def keys(report):
    return [i["key"] for s in report["sections"] for i in s["items"]]


def section(report, sid):
    return next((s for s in report["sections"] if s["id"] == sid), None)


# ---------------------------------------------------------------------- schedule
def test_first_report_at_report_time_and_only_once(tmp_path):
    clock = Clock(at(2026, 11, 15, 20, 0))
    c, rep = make(tmp_path, clock)
    rep.store.set_setting("report_epoch", at(2026, 11, 15, 7, 0))
    assert run(rep.run_due()) is None                          # installed after this morning's 06:30: nothing today
    clock.t = at(2026, 11, 16, 6, 29)
    assert run(rep.run_due()) is None
    clock.t = at(2026, 11, 16, 6, 30, 5)
    r = run(rep.run_due())
    assert r["day"] == "2026-11-16" and not r["catch_up"]
    assert r["since"] == at(2026, 11, 15, 7, 0) and r["until"] == clock.t
    assert run(rep.run_due()) is None
    clock.t = at(2026, 11, 17, 6, 31)
    r2 = run(rep.run_due())
    assert r2["day"] == "2026-11-17" and r2["since"] == r["until"]


def test_installed_this_morning_after_report_time_waits_for_tomorrow(tmp_path):
    clock = Clock(at(2026, 11, 16, 10, 0))
    c, rep = make(tmp_path, clock)
    rep.epoch()                                                # first start now
    clock.t = at(2026, 11, 16, 11, 0)
    assert run(rep.run_due()) is None
    clock.t = at(2026, 11, 17, 6, 30)
    r = run(rep.run_due())
    assert r["day"] == "2026-11-17" and r["since"] == at(2026, 11, 16, 10, 0)


def test_lanloom_was_off_at_report_time_catches_up_once(tmp_path):
    clock = Clock(at(2026, 11, 13, 6, 30))
    c, rep = make(tmp_path, clock)
    rep.store.set_setting("report_epoch", at(2026, 11, 1, 0, 0))
    first = run(rep.run_due())
    assert first["day"] == "2026-11-13"
    clock.t = at(2026, 11, 16, 9, 12)                          # off for three mornings, started at 09:12
    r = run(rep.run_due())
    assert r["day"] == "2026-11-16" and r["catch_up"] and r["long"]
    assert r["since"] == first["until"] and r["planned"] == at(2026, 11, 16, 6, 30)
    assert [x["day"] for x in rep.summary_rows()] == ["2026-11-16", "2026-11-13"]     # one report, not three


def test_just_after_midnight_belongs_to_yesterdays_morning(tmp_path):
    clock = Clock(at(2026, 11, 16, 0, 5))
    c, rep = make(tmp_path, clock)
    assert rep.due_day(clock.t).isoformat() == "2026-11-15"
    assert rep.next_at() == at(2026, 11, 16, 6, 30)


def test_report_time_follows_daylight_saving(tmp_path):
    clock = Clock(at(2026, 3, 29, 12, 0))
    c, rep = make(tmp_path, clock)
    from datetime import date
    assert datetime.fromtimestamp(rep.planned_at(date(2026, 3, 29)), Z).strftime("%H:%M") == "06:30"
    assert datetime.fromtimestamp(rep.planned_at(date(2026, 10, 25)), Z).strftime("%H:%M") == "06:30"


def test_clock_going_backwards_never_rewrites_history(tmp_path):
    clock = Clock(at(2026, 11, 16, 7, 0))
    c, rep = make(tmp_path, clock)
    rep.store.set_setting("report_epoch", at(2026, 11, 1))
    run(rep.run_due())
    clock.t = at(2026, 11, 14, 8, 0)
    assert run(rep.run_due()) is None


# ---------------------------------------------------------------------- empty and sparse sources
def test_no_sources_gives_a_short_clean_report(tmp_path):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    rep.store.set_setting("report_epoch", at(2026, 11, 1))
    r = run(rep.run_due())
    assert [s["id"] for s in r["sections"]] == ["lanloom"]
    assert r["points"] == 0 and r["level"] == "ok"
    rr = render.Renderer("en", Z, r["day"])
    assert render.headline(r, rr) == "All quiet"
    assert render.headline(r, render.Renderer("de", Z, r["day"])) == "Alles ruhig"


def test_sections_appear_only_with_their_source(tmp_path):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    add_device(c, 20, "Office laptop")
    r = rep.assemble(at(2026, 11, 15, 6, 30), clock.t, None, "2026-11-16", [])
    assert [s["id"] for s in r["sections"]] == ["devices", "lanloom"]
    assert section(r, "devices")["summary"]["key"] == "report.devices.calm"
    add_integration(c, "mqtt", "Message broker", {"host": "192.168.50.13"})
    r = rep.assemble(at(2026, 11, 15, 6, 30), clock.t, None, "2026-11-16", [{"id": "x", "name": "Broker", "days": 300}])
    assert [s["id"] for s in r["sections"]] == ["devices", "connections", "logins", "certificates", "lanloom"]


# ---------------------------------------------------------------------- journal watcher
def test_watch_journals_device_changes(tmp_path, monkeypatch):
    clock = Clock(at(2026, 11, 15, 20, 0))
    c, rep = make(tmp_path, clock)
    a = add_device(c, 20, "Office laptop")
    b = add_device(c, 2, "Core switch", kind="switch")
    status = {a: "up", b: "up"}
    monkeypatch.setattr(c, "device_status", lambda dev: status.get(dev["id"], "unknown"))
    c.cadence["presence"] = SimpleNamespace(last=clock.t)
    w = rep.watch
    w.started = clock.t - START_GRACE_S - 1
    assert w.observe(clock.t) == 0                              # first look: baseline, no "new" flood
    status[a] = "down"
    clock.t += 600
    w.observe(clock.t)
    status[a] = "up"
    clock.t += 600
    w.observe(clock.t)
    new = add_device(c, 77, "Guest phone", kind="phone")
    status[new] = "up"
    clock.t += 60
    w.observe(clock.t)
    status[b] = "down"
    clock.t += 60
    w.observe(clock.t)
    clock.t += GONE_AFTER_S + 60
    w.observe(clock.t)
    kinds = [(e["kind"], e["data"].get("title")) for e in rep.journal.between(0, clock.t)]
    assert kinds == [("dev_offline", "Office laptop"), ("dev_back", "Office laptop"), ("dev_new", "Guest phone"),
                     ("dev_offline", "Core switch"), ("dev_gone", "Core switch")]
    w.observe(clock.t + 60)                                     # gone is reported once per absence
    assert len(rep.journal.between(0, clock.t + 120)) == 5


def test_watch_books_nothing_for_a_device_marked_often_switched_off(tmp_path, monkeypatch):
    """lanloom#19: a television that leaves the network in standby on purpose is no outage - no slot, no event, no report
    line - and only a long absence (the days set on its card) is reported, once."""
    from lanloom import availability as av
    clock = Clock(at(2026, 11, 15, 20, 0))
    c, rep = make(tmp_path, clock)
    tv = add_device(c, 30, "Living room TV", kind="tv")
    lap = add_device(c, 20, "Office laptop")
    c.set_sleepy(tv, True, 2)
    status = {tv: "up", lap: "up"}
    monkeypatch.setattr(c, "device_status", lambda dev, *_: status.get(dev["id"], "unknown"))
    c.cadence["presence"] = SimpleNamespace(last=clock.t)
    w = rep.watch
    w.started = clock.t - START_GRACE_S - 1
    w.observe(clock.t)
    s0 = int(clock.t // av.SLOT_S)
    assert av.ups_of(c.availability.cells("dev:" + tv, s0, s0)[0]) >= 1      # a good look is still booked
    status[tv] = status[lap] = "down"
    for _ in range(2):
        clock.t += 600
        w.observe(clock.t)
    s = int(clock.t // av.SLOT_S)
    assert av.downs_of(c.availability.cells("dev:" + tv, s, s)[0]) == 0
    assert av.downs_of(c.availability.cells("dev:" + lap, s, s)[0]) >= 1
    kinds = [(e["kind"], e["data"]["id"]) for e in rep.journal.between(0, clock.t)]
    assert kinds == [("dev_offline", lap)]
    status[lap] = "up"
    clock.t += 2 * 86400 + 60
    w.observe(clock.t)
    long_off = [e for e in rep.journal.between(0, clock.t) if e["kind"] == "dev_long_off"]
    assert len(long_off) == 1 and long_off[0]["data"]["days"] == 2 and long_off[0]["data"]["id"] == tv
    clock.t += 60
    w.observe(clock.t)
    assert len([e for e in rep.journal.between(0, clock.t) if e["kind"] == "dev_long_off"]) == 1    # once per absence
    assert not [e for e in rep.journal.between(0, clock.t) if e["data"].get("id") == tv and e["kind"] in ("dev_offline", "dev_gone")]


def test_marked_device_back_after_a_short_nap_writes_no_back_row(tmp_path, monkeypatch):
    clock = Clock(at(2026, 11, 15, 20, 0))
    c, rep = make(tmp_path, clock)
    tv = add_device(c, 30, "Living room TV", kind="tv")
    c.set_sleepy(tv, True, 0)                                  # 0 = never warn
    status = {tv: "up"}
    monkeypatch.setattr(c, "device_status", lambda dev, *_: status.get(dev["id"], "unknown"))
    c.cadence["presence"] = SimpleNamespace(last=clock.t)
    w = rep.watch
    w.started = clock.t - START_GRACE_S - 1
    w.observe(clock.t)
    status[tv] = "down"
    clock.t += 30 * 86400
    w.observe(clock.t)
    clock.t += 60
    w.observe(clock.t)
    status[tv] = "up"
    clock.t += 60
    w.observe(clock.t)
    assert rep.journal.between(0, clock.t) == []


def test_an_outage_open_before_the_mark_still_gets_its_back_row(tmp_path, monkeypatch):
    """Marking a device that is already reported offline must not leave that outage (and its notification) open forever."""
    clock = Clock(at(2026, 11, 15, 20, 0))
    c, rep = make(tmp_path, clock)
    tv = add_device(c, 30, "Living room TV", kind="tv")
    status = {tv: "up"}
    monkeypatch.setattr(c, "device_status", lambda dev, *_: status.get(dev["id"], "unknown"))
    c.cadence["presence"] = SimpleNamespace(last=clock.t)
    w = rep.watch
    w.started = clock.t - START_GRACE_S - 1
    w.observe(clock.t)
    status[tv] = "down"
    clock.t += 60
    w.observe(clock.t)                                          # dev_offline, before anyone marked it
    c.set_sleepy(tv, True)
    clock.t += 60
    w.observe(clock.t)
    status[tv] = "up"
    clock.t += 60
    w.observe(clock.t)
    assert [e["kind"] for e in rep.journal.between(0, clock.t)] == ["dev_offline", "dev_back"]


def test_devices_section_reports_a_long_absence_of_a_marked_device(tmp_path, monkeypatch):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    tv = add_device(c, 30, "Living room TV", kind="tv")
    monkeypatch.setattr(c, "device_status", lambda dev, *_: "down")
    rep.journal.add("dev_long_off", {"id": tv, "title": "Living room TV", "kind": "tv", "since": at(2026, 11, 2, 21, 0),
                                     "days": 14}, at(2026, 11, 16, 3, 0))
    r = rep.assemble(at(2026, 11, 15, 6, 30), clock.t, None, "2026-11-16", [])
    sec = section(r, "devices")
    rr = render.Renderer("en", Z, "2026-11-16")
    lines = {i["key"]: rr.line(i) for i in sec["items"]}
    assert lines["report.devices.long_off"].startswith("Living room TV is marked as often switched off and has not been seen since")
    assert lines["report.devices.long_off"].endswith("(14 days).")
    assert [i["key"] for i in sec["items"] if i["point"]] == ["report.devices.long_off"]


def test_watch_waits_after_a_start_and_survives_a_restart(tmp_path, monkeypatch):
    clock = Clock(at(2026, 11, 15, 20, 0))
    c, rep = make(tmp_path, clock)
    a = add_device(c, 20, "Office laptop")
    status = {a: "up"}
    monkeypatch.setattr(c, "device_status", lambda dev: status[dev["id"]])
    c.cadence["presence"] = SimpleNamespace(last=clock.t)
    rep.watch.started = clock.t
    status[a] = "down"
    rep.watch.observe(clock.t + 10)                             # inside the start grace: presence knows nothing yet
    assert rep.journal.between(0, clock.t + 100) == []
    rep.watch.started = clock.t - START_GRACE_S
    status[a] = "up"
    rep.watch.observe(clock.t + 20)
    status[a] = "down"
    rep.watch.observe(clock.t + 30)
    # a new process (restart) reads the saved state and still knows the laptop was up before
    rep2 = Reports(c, Path(tmp_path), clock=clock)
    rep2.watch.started = clock.t - START_GRACE_S
    status[a] = "up"
    rep2.watch.observe(clock.t + 40)
    assert [e["kind"] for e in rep2.journal.between(0, clock.t + 100)] == ["dev_offline", "dev_back"]


def test_watch_journals_connections_and_the_uplink(tmp_path):
    clock = Clock(at(2026, 11, 15, 20, 0))
    c, rep = make(tmp_path, clock)
    iid = add_integration(c, "hub", "Automation hub")
    sid = add_integration(c, "snmp", "Router counters", {"host": "192.168.50.1"})
    c.gateway_ip = "192.168.50.1"
    key = "int:%d" % iid

    def snmp(oper, t):
        c.snmp[sid] = {"host": "192.168.50.1", "t": t, "ifaces": [{"name": "wan", "oper": oper, "in": 1, "out": 1}]}

    c.source_status[key] = {"ok": True, "key": "source.ok", "t": clock.t}
    snmp("up", clock.t)
    rep.watch.observe(clock.t)
    c.source_status[key] = {"ok": False, "key": "source.hub.auth_failed", "t": clock.t}
    snmp("down", clock.t + 60)
    rep.watch.observe(clock.t + 60)
    c.source_status[key] = {"ok": True, "key": "source.ok", "t": clock.t}
    snmp("up", clock.t + 120)
    rep.watch.observe(clock.t + 120)
    snmp("down", clock.t - 1000)                                # stale counters say nothing about the line
    rep.watch.observe(clock.t + 900)
    got = [(e["kind"], e["data"].get("key")) for e in rep.journal.between(0, clock.t + 1000)]
    assert got == [("src_fail", "source.hub.auth_failed"), ("wan_down", None), ("src_ok", None), ("wan_up", None)]


def test_intervals_with_open_end_and_down_at_start():
    evs = [{"t": 10, "kind": "x_up"}, {"t": 20, "kind": "x_down"}, {"t": 30, "kind": "x_up"}, {"t": 40, "kind": "x_down"}]
    assert intervals(evs, "x_down", "x_up", 0, 50, True) == [(0, 10, False), (20, 30, False), (40, 50, True)]
    assert intervals([], "x_down", "x_up", 0, 50, False) == []


# ---------------------------------------------------------------------- sections
def test_internet_section_outages_flap_and_open_outage(tmp_path):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    j = rep.journal
    j.add("wan_down", {"name": "wan"}, at(2026, 11, 16, 3, 12))
    j.add("wan_up", {"name": "wan"}, at(2026, 11, 16, 3, 26))
    j.add("wan_down", {"name": "wan"}, at(2026, 11, 16, 4, 0))
    j.add("wan_up", {"name": "wan"}, at(2026, 11, 16, 4, 0, 40))
    j.add("wan_switch", {"from": "Fibre", "to": "Mobile"}, at(2026, 11, 16, 5, 0))
    j.add("wan_down", {"name": "wan"}, at(2026, 11, 16, 6, 0))
    r = rep.assemble(at(2026, 11, 15, 6, 30), clock.t, None, "2026-11-16", [])
    sec = section(r, "internet")
    assert sec["summary"]["key"] == "report.internet.summary" and sec["summary"]["vars"]["n"] == 3
    pts = [i for i in sec["items"] if i["point"]]
    assert [i["key"] for i in pts] == ["report.internet.down_now", "report.internet.outage"]   # the 40 s flap is a note only
    assert sec["level"] == "danger"
    rr = render.Renderer("en", Z, "2026-11-16")
    assert rr.line(pts[1]) == "Down from 03:12 to 03:26 (14 min)."
    assert rr.line(next(i for i in sec["items"] if i["key"] == "report.internet.switch")) == "Switched to Mobile at 05:00 (was Fibre)."
    # an outage that began before the window counts from the window start
    r2 = rep.assemble(at(2026, 11, 16, 6, 10), clock.t, None, "2026-11-16", [])
    assert section(r2, "internet")["items"][0]["vars"]["from_at"] == at(2026, 11, 16, 6, 10)


def test_devices_section(tmp_path, monkeypatch):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    lap = add_device(c, 20, "Office laptop")
    sw = add_device(c, 2, "Core switch", kind="switch")
    tab = add_device(c, 23, "Kitchen tablet", kind="phone")
    status = {lap: "up", sw: "down", tab: "up"}
    monkeypatch.setattr(c, "device_status", lambda dev: status.get(dev["id"], "unknown"))
    j = rep.journal
    j.add("dev_new", {"id": "d-x", "title": "Guest phone", "ip": "192.168.50.77", "kind": "phone"}, at(2026, 11, 15, 23, 0))
    j.add("dev_offline", {"id": lap, "title": "Office laptop", "kind": "computer"}, at(2026, 11, 16, 0, 30))
    j.add("dev_back", {"id": lap, "title": "Office laptop", "kind": "computer"}, at(2026, 11, 16, 5, 30))
    j.add("dev_offline", {"id": sw, "title": "Core switch", "kind": "switch"}, at(2026, 11, 16, 2, 0))
    for h_ in (1, 3, 5):
        j.add("dev_offline", {"id": tab, "title": "Kitchen tablet", "kind": "phone"}, at(2026, 11, 16, h_, 0))
        j.add("dev_back", {"id": tab, "title": "Kitchen tablet", "kind": "phone"}, at(2026, 11, 16, h_, 20))
    c.store.execute("UPDATE devices SET user_title='Laptop (office)' WHERE id=?", (lap,))     # renamed since
    r = rep.assemble(at(2026, 11, 15, 6, 30), clock.t, None, "2026-11-16", [])
    sec = section(r, "devices")
    rr = render.Renderer("en", Z, "2026-11-16")
    lines = {i["key"]: rr.line(i) for i in sec["items"]}
    assert lines["report.devices.new"] == "New device: Guest phone (192.168.50.77), first seen at Sun 15 Nov 23:00."
    assert lines["report.devices.still_offline"] == "Core switch went offline at 02:00 and is still offline."
    assert lines["report.devices.offline_back"] == "Laptop (office) was offline from 00:30 to 05:30."
    assert lines["report.devices.offline_times"].startswith("Kitchen tablet went offline 3 times")
    assert [i["key"] for i in sec["items"] if i["point"]] == ["report.devices.new", "report.devices.still_offline"]
    assert rr.line(sec["summary"]) == "3 devices · new: 1 · still offline: 1 · briefly offline: 2"


def test_connections_and_sign_ins(tmp_path):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    hub = add_integration(c, "hub", "Automation hub")
    brk = add_integration(c, "mqtt", "Message broker")
    j = rep.journal
    j.add("src_fail", {"id": "int:%d" % brk, "name": "Message broker", "key": "source.mqtt.unreachable"}, at(2026, 11, 16, 2, 0))
    j.add("src_ok", {"id": "int:%d" % brk, "name": "Message broker"}, at(2026, 11, 16, 2, 6))
    j.add("src_fail", {"id": "int:%d" % hub, "name": "Automation hub", "key": "source.hub.auth_failed"}, at(2026, 11, 16, 4, 0))
    c.source_status["int:%d" % hub] = {"ok": False, "key": "source.hub.auth_failed", "t": clock.t}
    r = rep.assemble(at(2026, 11, 15, 6, 30), clock.t, None, "2026-11-16", [])
    conn = section(r, "connections")
    assert {i["key"] for i in conn["items"]} == {"report.conn.interrupted", "report.conn.down_now"}
    login = section(r, "logins")
    assert login["items"][0]["key"] == "report.login.refused" and login["items"][0]["point"]
    rr = render.Renderer("en", Z, "2026-11-16")
    assert rr.line(login["items"][0]) == ("Automation hub refuses the sign-in (Credentials were rejected). "
                                          "Check the account in Settings › Connections.")
    c.source_status["int:%d" % hub] = {"ok": True, "key": "source.ok", "t": clock.t}
    r = rep.assemble(at(2026, 11, 15, 6, 30), clock.t, None, "2026-11-16", [])
    assert section(r, "logins")["summary"]["key"] == "report.login.recovered"


def test_certificates_thresholds(tmp_path):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    certs = [{"id": "a", "name": "Controller", "days": 11}, {"id": "b", "name": "Broker", "days": 23},
             {"id": "c", "name": "Storage", "days": 200}, {"id": "d", "name": "Hub", "days": -3}, {"id": "e", "name": "Old", "days": None}]
    r = rep.assemble(0, clock.t, None, "2026-11-16", certs)
    sec = section(r, "certificates")
    got = {i["key"]: i["point"] for i in sec["items"]}
    assert got == {"report.cert.soon": True, "report.cert.month": False, "report.cert.expired": True, "report.cert.unread": False}
    assert sec["level"] == "danger" and sec["summary"]["vars"] == {"n": 5, "ok": 1}


def test_controller_changes_against_the_previous_report(tmp_path):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    iid = add_integration(c, "sample", "Sample controller")
    st = {"policies": [{"id": "p1", "name": "Block guests", "action": "block", "hits": 3}],
          "networks": [{"id": "n1", "name": "Home"}], "equipment": [{"id": "e1", "name": "Gateway", "firmware": "4.2.1"}]}
    c.live_cfg[iid] = {"state": st, "caps": {}, "family": "sample", "t": time.time()}   # freshness is judged on the wall clock
    first = rep.assemble(0, clock.t, None, "2026-11-15", [])
    assert section(first, "controller")["summary"]["key"] == "report.ctl.baseline"
    st2 = json.loads(json.dumps(st))
    st2["policies"][0]["hits"] = 99                               # counters are no change
    c.live_cfg[iid]["state"] = st2
    same = rep.assemble(0, clock.t, first, "2026-11-16", [])
    assert section(same, "controller")["summary"]["key"] == "report.ctl.calm"
    st2["policies"][0]["action"] = "allow"
    st2["networks"].append({"id": "n2", "name": "Lab"})
    st2["equipment"][0]["firmware"] = "4.2.2"
    diff = rep.assemble(0, clock.t, first, "2026-11-16", [])
    sec = section(diff, "controller")
    assert sorted(i["key"] for i in sec["items"]) == ["report.ctl.added", "report.ctl.changed", "report.ctl.firmware"]
    rr = render.Renderer("en", Z, "2026-11-16")
    assert rr.line(next(i for i in sec["items"] if i["key"] == "report.ctl.added")) == "Sample controller: new networks (1): Lab."


def test_lanloom_section_update_restart_and_low_disk(tmp_path, monkeypatch):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    rep.update_state = lambda: {"current": "1.6.0", "latest": "1.7.0", "available": True}
    rep.journal.add("app_start", {"version": "1.7.0", "prev": "1.6.0", "down_s": 40}, at(2026, 11, 16, 3, 0))
    rep.journal.add("app_start", {"version": "1.7.0", "down_s": 300}, at(2026, 11, 16, 4, 0))
    monkeypatch.setattr(contrib.shutil, "disk_usage", lambda p: SimpleNamespace(total=100_000_000_000, used=0, free=2_000_000_000))
    r = rep.assemble(at(2026, 11, 15, 6, 30), clock.t, None, "2026-11-16", [])
    sec = section(r, "lanloom")
    got = {i["key"]: (i["point"], i["level"]) for i in sec["items"]}
    assert got["report.self.update"] == (True, "info")
    assert got["report.self.updated"] == (False, "ok") and got["report.self.restart"] == (False, "ok")   # maintenance, no fault
    assert got["report.self.disk_low"][0] is True


def test_register_new_contribution_and_failing_section(tmp_path):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    calls = []
    contrib.register(contrib.Contribution("zz-test", 70, "report.sec.lanloom", lambda ctx: True,
                                          lambda ctx: contrib.section("zz-test", "report.sec.lanloom", "report.self.calm",
                                                                      [contrib.item("zz:1", "report.self.started", "warn", True, at_at=ctx.until)])))
    contrib.register(contrib.Contribution("zz-off", 71, "report.sec.lanloom", lambda ctx: False, lambda ctx: calls.append(1)))
    contrib.register(contrib.Contribution("zz-broken", 72, "report.sec.lanloom", lambda ctx: True, lambda ctx: 1 / 0))
    try:
        r = rep.assemble(0, clock.t, None, "2026-11-16", [])
        ids = [s["id"] for s in r["sections"]]
        assert ids == ["zz-test", "zz-broken", "lanloom"] and not calls
        assert section(r, "zz-broken")["summary"]["key"] == "report.section_failed"
        assert r["points"] == 1
    finally:
        contrib.REGISTRY[:] = [x for x in contrib.REGISTRY if not x.id.startswith("zz-")]


def test_long_sections_fold_notes_but_keep_points(tmp_path):
    items = [contrib.item("n%d" % i, "report.self.started") for i in range(20)] + [contrib.item("p", "report.self.update", "info", True)]
    sec = contrib.section("x", "t", "s", items)
    assert sec["items"][0]["id"] == "p" and sec["more"] == len(items) - len(sec["items"])
    assert len(sec["items"]) == contrib.MAX_ITEMS


# ---------------------------------------------------------------------- archive, tick marks, settings
def test_tick_marks_read_state_and_archive(tmp_path):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    rep.store.set_setting("report_epoch", at(2026, 10, 1))
    rep.update_state = lambda: {"current": "1.6.0", "latest": "1.7.0", "available": True}
    r = run(rep.run_due())
    assert rep.unread() == 1 and r["points"] == 1
    item_id = section(r, "lanloom")["items"][0]["id"]
    got = rep.mark_item(r["day"], item_id, True)
    assert got["open"] == 0 and section(got, "lanloom")["items"][0]["done"] == clock.t
    with pytest.raises(ValueError):
        rep.mark_item(r["day"], "nope", True)
    assert rep.mark_item(r["day"], item_id, False)["open"] == 1
    assert rep.mark_read(r["day"]) and rep.unread() == 0
    rep.store.set_setting("report_cfg", {"time": "06:30", "tz": TZ, "keep_days": 7})
    clock.t = at(2026, 11, 24, 6, 30)
    run(rep.run_due())
    assert [x["day"] for x in rep.summary_rows()] == ["2026-11-24"]          # the old one left the 7-day archive
    assert rep.store.one("SELECT COUNT(*) AS n FROM report_marks")["n"] == 0


def test_settings_validation_and_secrets_stay_in_the_vault(tmp_path):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    for patch, key in (({"time": "6:30"}, "report.err.time"), ({"tz": "Mars/Base"}, "report.err.tz"), ({"keep_days": 5}, "report.err.keep"),
                       ({"link": "ftp://x"}, "report.err.link")):
        with pytest.raises(ValueError) as e:
            rep.update_settings(patch)
        assert str(e.value) == key
    with pytest.raises(ValueError) as e:
        rep.update_settings({"channels": {"chatbot": {"enabled": True, "chat_id": "12345"}}})
    assert str(e.value) == "report.err.channel_incomplete"
    token = "123456789:" + "A" * 35
    out = rep.update_settings({"channels": {"chatbot": {"enabled": True, "chat_id": "12345"}}}, {"chatbot.token": token})
    assert out["channels"]["chatbot"]["secrets"] == {"token": True}
    raw = json.dumps(rep.store.get_setting("report_cfg")) + json.dumps(out)
    assert token not in raw
    with pytest.raises(ValueError):
        rep.update_settings({}, {"chatbot.token": "not a token"})
    rep.update_settings({"channels": {"chatbot": {"enabled": False}}}, {"chatbot.token": ""})
    assert rep.public_settings()["channels"]["chatbot"]["secrets"] == {"token": False}


def test_time_zone_falls_back_to_the_browser_zone(tmp_path, monkeypatch):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    monkeypatch.delenv("TZ", raising=False)
    rep.store.set_setting("report_cfg", {"time": "06:30"})
    assert rep.tz_name() == "UTC"
    rep.set_auto_tz("Europe/Lisbon")
    rep.set_auto_tz("No/Such_Zone")
    assert rep.tz_name() == "Europe/Lisbon"


# ---------------------------------------------------------------------- delivery
def sample_report():
    return {"day": "2026-11-16", "points": 2, "sections": [
        {"id": "internet", "title": "report.sec.internet", "summary": {"key": "report.internet.none", "vars": {}}, "items": [
            {"id": "wan:1", "key": "report.internet.outage", "point": True, "level": "warn",
             "vars": {"from_at": at(2026, 11, 16, 3, 12), "to_at": at(2026, 11, 16, 3, 26), "dur_dur": 840}}]},
        {"id": "devices", "title": "report.sec.devices", "summary": {"key": "report.devices.calm", "vars": {"n": 3}}, "items": [
            {"id": "new:d-secret-id", "key": "report.devices.new", "point": True, "level": "warn",
             "vars": {"name": "Guest phone", "ip": "192.168.50.77", "at_at": at(2026, 11, 16, 1, 0)}}]}]}


def test_short_text_is_three_lines_and_a_link():
    msg = render.short_text(sample_report(), "en", Z, "http://192.168.50.11:7380")
    lines = msg["text"].split("\n")
    assert lines == ["2 things need a look", "Internet: Down from 03:12 to 03:26 (14 min).",
                     "Devices: New device: Guest phone (192.168.50.77), first seen at 01:00.",
                     "http://192.168.50.11:7380/#/reports/2026-11-16"]
    assert msg["title"] == "Lanloom night report · 16 Nov 2026"
    assert "d-secret-id" not in json.dumps(msg)
    de = render.short_text(sample_report(), "de", Z, "")
    assert de["title"] == "Lanloom-Nachtbericht · 16.11.2026" and de["text"].split("\n")[0] == "2 Dinge brauchen einen Blick"
    assert len(de["text"].split("\n")) == 3


def test_short_text_in_french_spanish_dutch_and_italian():
    """O6: the delivered report reads in every language: title with the date as the language writes it, plural head, three lines."""
    want = {"fr": ("22/11/2026", "2 points"), "es": ("22/11/2026", "2"), "nl": ("22-11-2026", "2"), "it": ("22/11/2026", "2")}
    for lang in ("fr", "es", "nl", "it"):
        msg = render.short_text(sample_report(), lang, Z, "")
        assert render.fmt_date("2026-11-22", lang) == want[lang][0], lang
        assert msg["title"].endswith(render.fmt_date("2026-11-16", lang)) and "{" not in msg["text"] and "#" not in msg["text"], lang
        assert len(msg["text"].split("\n")) == 3, lang
        assert "Down" not in msg["text"] and "New device" not in msg["text"], "%s: English text left in the report" % lang
        wd = render.fmt_time(at(2026, 11, 15, 23, 30), Z, "2026-11-16", lang)
        assert wd.endswith("23:30") and wd.split()[0] in render.WEEKDAYS[lang], (lang, wd)
        assert render.WEEKDAYS[lang] and len(render.WEEKDAYS[lang]) == 7 and len(render.MONTHS.get(lang, ())) in (0, 12)
    assert render.fmt_date("2026-11-22", "en") == "22 Nov 2026" and render.fmt_date("2026-11-22", "de") == "22.11.2026"


class _Hook(BaseHTTPRequestHandler):
    got = []
    code = 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Hook.got.append((self.path, dict(self.headers), self.rfile.read(n)))
        self.send_response(_Hook.code)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def hook():
    _Hook.got, _Hook.code = [], 200
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_webhook_and_push_topic_delivery(hook):
    msg = render.short_text(sample_report(), "en", Z, "")
    ok, key = deliver.send("webhook", {"enabled": True}, {"url": hook + "/hook/abc"}, msg)
    assert (ok, key) == (True, "report.send.ok")
    path, headers, body = _Hook.got[-1]
    data = json.loads(body)
    assert path == "/hook/abc" and data["headline"] == "2 things need a look" and data["points"] == 2 and data["link"] is None
    ok, key = deliver.send("ntfy", {"server": hook}, {"topic": "lanloom-test", "token": "tk-123"}, msg)
    path, headers, body = _Hook.got[-1]
    assert ok and path == "/lanloom-test" and headers["Authorization"] == "Bearer tk-123" and body.decode().startswith("2 things")
    _Hook.code = 403
    assert deliver.send("webhook", {}, {"url": hook + "/x?token=SECRET"}, msg) == (False, "report.send.auth")
    _Hook.code = 400
    assert deliver.send("webhook", {}, {"url": hook + "/x"}, msg) == (False, "report.send.rejected")


def test_unreachable_and_bot_and_mail(monkeypatch):
    msg = render.short_text(sample_report(), "en", Z, "")
    assert deliver.send("webhook", {}, {"url": "http://127.0.0.1:9/x"}, msg) == (False, "report.send.unreachable")
    sent = []
    monkeypatch.setattr(deliver, "_post", lambda url, body, headers: sent.append((url, json.loads(body))))
    ok, _ = deliver.send("chatbot", {"chat_id": "12345"}, {"token": "123:abc"}, msg)
    assert ok and sent[0][0] == deliver.BOT_API + "/bot123:abc/sendMessage" and sent[0][1]["chat_id"] == "12345"

    class FakeSMTP:
        inst = None

        def __init__(self, host, port, timeout=None):
            self.calls = [("connect", host, port)]
            FakeSMTP.inst = self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            self.calls.append(("starttls",))

        def login(self, u, p):
            self.calls.append(("login", u))

        def send_message(self, m, to_addrs=None):
            self.calls.append(("send", m["Subject"], tuple(to_addrs)))

    monkeypatch.setattr(deliver.smtplib, "SMTP", FakeSMTP)
    cfg = deliver.clean_channel("email", {"enabled": True, "host": "mail.example.org", "port": 587, "security": "starttls",
                                          "username": "lanloom", "sender": "lanloom@example.org", "to": "a@example.org; b@example.org"})
    ok, key = deliver.send("email", cfg, {"password": "pw"}, msg)
    assert ok and FakeSMTP.inst.calls == [("connect", "mail.example.org", 587), ("starttls",), ("login", "lanloom"),
                                          ("send", "Lanloom night report · 16 Nov 2026", ("a@example.org", "b@example.org"))]


def test_failed_delivery_is_retried_quietly(tmp_path, monkeypatch):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    rep.store.set_setting("report_epoch", at(2026, 10, 1))
    rep.update_settings({"channels": {"webhook": {"enabled": True}}}, {"webhook.url": "http://127.0.0.1:9/hook"})
    results = [(False, "report.send.unreachable"), (True, "report.send.ok")]
    monkeypatch.setattr(deliver, "send", lambda ch, cfg, sec, msg: results.pop(0))
    r = run(rep.run_due())
    assert rep.get(r["day"])["delivery"]["webhook"]["ok"] is False
    clock.t += 60
    run(rep.retry_deliveries(clock.t))                         # too early
    assert results
    clock.t += 1000
    run(rep.retry_deliveries(clock.t))
    assert rep.get(r["day"])["delivery"]["webhook"]["ok"] is True and not results


def test_nothing_leaves_without_a_channel(tmp_path, monkeypatch):
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    rep.store.set_setting("report_epoch", at(2026, 10, 1))
    monkeypatch.setattr(deliver, "send", lambda *a: pytest.fail("no channel is set up"))
    r = run(rep.run_due())
    assert rep.get(r["day"])["delivery"] == {}


# ---------------------------------------------------------------------- demo data
def test_demo_has_three_mornings_with_every_section(tmp_path, monkeypatch):
    from lanloom.demo import Demo
    monkeypatch.setenv("TZ", TZ)
    clock = Clock(at(2026, 11, 16, 9, 0))
    c, rep = make(tmp_path, clock, demo=True)
    c.demo = Demo()
    c.demo.seed(c)
    c.demo.seed_integrations(c)
    c.demo.seed_reports(c, now=clock.t)
    rows = rep.summary_rows()
    assert [r["day"] for r in rows] == ["2026-11-16", "2026-11-15", "2026-11-14"]
    assert [r["read"] for r in rows] == [False, True, True] and rows[2]["open"] == 0
    newest = rep.get("2026-11-16")
    ids = [s["id"] for s in newest["sections"]]
    for sid in ("devices", "connections", "logins", "paths", "certificates", "lanloom"):
        assert sid in ids
    assert "internet" in [s["id"] for s in rep.get("2026-11-15")["sections"]]
    text = json.dumps([rep.get(r["day"]) for r in rows])
    assert "192.168.50." in text and "10.0." not in text


# ---------------------------------------------------------------------- HTTP API
def test_reports_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from lanloom import config as config_mod
    from lanloom.main import create_app
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    hdr = {"X-Lanloom": "1"}
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as cl:
        for path in ("/api/reports", "/api/reports/preview", "/api/reports/settings", "/api/reports/2026-11-16"):
            assert cl.get(path).status_code == 401, path
        code = (tmp_path / "setup-code.txt").read_text().strip()
        assert cl.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"},
                       headers=hdr).status_code == 200
        st = cl.get("/api/state").json()
        assert st["reports_unread"] == 1 and st["report_tz_auto"] == ""
        lst = cl.get("/api/reports").json()
        assert len(lst["reports"]) == 3 and lst["unread"] == 1
        day = lst["reports"][0]["day"]
        rep = cl.get("/api/reports/" + day).json()
        assert "controller_snapshot" not in rep and rep["open"] == rep["points"] > 0
        point = next(i for s in rep["sections"] for i in s["items"] if i["point"])
        r = cl.put("/api/reports/%s/items" % day, json={"item": point["id"], "done": True}, headers=hdr).json()
        assert r["open"] == rep["open"] - 1
        assert cl.put("/api/reports/%s/items" % day, json={"item": "nope", "done": True}, headers=hdr).json()["error"] == "report.err.item"
        assert cl.post("/api/reports/%s/read" % day, json={}, headers=hdr).json() == {"ok": True, "unread": 0}
        assert cl.get("/api/reports/1999-01-01").status_code == 404
        assert cl.get("/api/reports/../../etc").status_code == 404
        pre = cl.get("/api/reports/preview").json()
        assert pre["preview"] is True and len(cl.get("/api/reports").json()["reports"]) == 3      # a preview is never saved
        assert cl.post("/api/reports/tz", json={"tz": "Europe/Lisbon"}, headers=hdr).json()["ok"]
        assert cl.get("/api/state").json()["report_tz_auto"] == "Europe/Lisbon"
        bad = cl.put("/api/reports/settings", json={"settings": {"time": "25:00"}}, headers=hdr)
        assert bad.status_code == 400 and bad.json()["error"] == "report.err.time"
        ok = cl.put("/api/reports/settings", json={"settings": {"time": "07:15", "keep_days": 90}}, headers=hdr).json()
        assert ok["time"] == "07:15" and ok["keep_days"] == 90
        t = cl.post("/api/reports/test", json={"channel": "ntfy"}, headers=hdr).json()
        assert t == {"ok": False, "key": "report.err.channel_incomplete"}


def test_device_family_sections_from_the_integration_register(tmp_path, monkeypatch):
    from lanloom import integrations
    clock = Clock(at(2026, 11, 16, 6, 30))
    c, rep = make(tmp_path, clock)
    iid = add_integration(c, "fam-x", "Sample hypervisor")
    seen = []

    def night(prev, state, now, name):
        seen.append((prev, now, name))
        items = [{"level": "alert", "key": "night.hv.node_offline", "params": {"host": "node-1"}},
                 {"level": "info", "key": "night.hv.backup_ok", "params": {"n": 2}}]
        return {"family": "fam-x", "title": "night.hv.title", "summary": "night.hv.summary_look", "params": {"n": 1}, "items": items}

    monkeypatch.setattr(integrations, "night_sections", lambda: [("fam-x", night)])
    c.live_cfg[iid] = {"state": {"hosts": [{"name": "node-1", "online": False}], "guests": [], "ports": [1] * 50}, "caps": {},
                       "family": "fam-x", "t": time.time()}
    first = rep.assemble(at(2026, 11, 15, 6, 30), clock.t, None, "2026-11-16", [])
    sec = section(first, "family:int:%d" % iid)
    assert sec["name"] == "Sample hypervisor" and sec["level"] == "danger"
    assert [(i["key"], i["point"]) for i in sec["items"]] == [("night.hv.node_offline", True), ("night.hv.backup_ok", False)]
    assert seen[0] == (None, clock.t, "Sample hypervisor")
    stored = first["family_states"]["int:%d" % iid]["state"]
    assert "ports" not in stored and stored["t"] == clock.t             # big lists stay out of the archive
    rep.assemble(clock.t, clock.t + 86400, first, "2026-11-17", [])
    assert seen[1][0]["hosts"][0]["name"] == "node-1"                   # the next report gets last night's state
    c.live_cfg.clear()
    assert section(rep.assemble(0, clock.t, first, "2026-11-17", []), "family:int:%d" % iid) is None   # no source, no section
