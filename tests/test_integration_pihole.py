"""A DNS filter in the whole app (one product as the example): the connection kind, the reading in the core, the map component,
the card, the device's section, the routes, the night report, the setup assistant's suggestion, the demo."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import core as core_mod
from lanloom import demo, demo_dns, dnsfilter, integrations, mapmodel
from lanloom.integrations import catalog
from lanloom.integrations.pihole import model as ph_model
from lanloom.main import create_app
from lanloom.nightly import contrib
from lanloom.store import Store

from integrations.pihole import fixtures as fx
from integrations.pihole.fake import PASSWORD, FakePihole

HDR = {"X-Lanloom": "1"}


def fresh_core():
    st = Store(":memory:")
    return core_mod.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), st, None)


def add_device(c, n, ip, title="laptop"):
    c.ingest({"hosts": [{"ip": ip, "mac": "02:4c:4c:00:00:%02x" % n, "hostnames": [title], "ports": [], "mdns": [], "ssdp": [], "sources": ["arp"]}]}, None)
    c._refresh_index()
    return c.ip_to_device[ip]


def connect(c, url="http://192.0.2.2"):
    integ = c.save_integration("pihole", "Pi-hole", {"url": url}, {"password": "x"})
    return integ["id"]


def reading_event(now=None):
    now = now or time.time()
    st = ph_model.to_neutral(dict(fx.DATA, history=fx.pihole_history(now)))
    st["t"] = now
    st["lists_updated"] = int(now - 86400)
    return {"type": "dns", "source": "family", "family": "pihole", "t": now, "state": st}


# ---------------------------------------------------------------------------- the core
def test_a_reading_is_booked_with_its_sums_and_the_map_gets_a_component():
    c = fresh_core()
    dev = add_device(c, 10, "192.0.2.10")
    iid = connect(c)
    c.on_event("int:%d" % iid, iid, reading_event())
    assert c.dns[iid]["state"]["totals"]["queries"] == 1440 and c.dns_log.has_data(iid)
    assert sum(r[1] for r in c.dns_log.hours(iid, 30)) == 24 * 6 * 10
    comps = {x["id"]: x for x in mapmodel.build(c, "overview")["components"]}
    comp = comps["dns:%d" % iid]                                    # the filter's address is no scanned device: a component of its own
    assert comp["badges"] == ["DNS"] and comp["kind"] == "service" and comp["title"] == "Pi-hole"
    grades, metrics = mapmodel.live_overlay(c, time.time(), {})
    assert metrics["dns:%d" % iid] == "30 %" and "dns:%d" % iid in grades
    card = mapmodel.component_detail(c, "dns:%d" % iid)
    v = card["dns"][0]
    assert v["health"] == "ok" and v["totals"]["share"] == 0.3 and [t["domain"] for t in v["top_blocked"]][0] == "tracker.example"
    assert v["top_clients"][0]["device"] == dev and v["top_clients"][0]["title"]
    mine = mapmodel.component_detail(c, dev)["dns_client"]["filters"][0]
    assert mine["q"] == 900 and mine["share_of_all"] == 0.625 and round(mine["blocked_share"], 2) == 0.33


def test_a_filter_on_a_known_device_marks_that_device_instead():
    c = fresh_core()
    dev = add_device(c, 2, "192.0.2.2", "filter-host")
    iid = connect(c)
    c.on_event("int:%d" % iid, iid, reading_event())
    comps = {x["id"]: x for x in mapmodel.build(c, "overview")["components"]}
    assert "dns:%d" % iid not in comps and "DNS" in comps[dev]["badges"]
    assert mapmodel.component_detail(c, dev)["dns"][0]["integ"] == iid


def test_a_bad_reading_is_a_protocol_error_and_nothing_is_stored():
    c = fresh_core()
    iid = connect(c)
    c.on_event("int:%d" % iid, iid, {"type": "dns", "source": "family", "family": "pihole", "state": {"v": 1, "hours": "x"}})
    assert iid not in c.dns and c.source_status["int:%d" % iid]["key"] == "source.family.protocol" and not c.dns_log.has_data(iid)


def test_deleting_the_connection_deletes_its_sums():
    c = fresh_core()
    iid = connect(c)
    c.on_event("int:%d" % iid, iid, reading_event())
    assert c.delete_integration(iid)
    assert iid not in c.dns and not c.dns_log.has_data(iid)


def test_a_stale_filter_is_reported_as_such():
    c = fresh_core()
    iid = connect(c)
    c.on_event("int:%d" % iid, iid, reading_event())
    assert dnsfilter.filter_view(c, iid, time.time() + 3600)["health"] == "stale"
    c.source_status["int:%d" % iid] = {"ok": False, "key": "source.family.unreachable", "t": time.time()}
    assert dnsfilter.filter_view(c, iid)["health"] == "down"


def test_a_filter_before_its_first_reading_waits():
    c = fresh_core()
    iid = connect(c)
    v = dnsfilter.filter_view(c, iid)
    assert v["health"] == "waiting" and v["totals"] is None and v["findings"] == []
    assert dnsfilter.filter_view(c, 999) is None


# ---------------------------------------------------------------------------- the connection test and the real read half
def test_the_dialog_test_reaches_a_stand_in():
    srv = FakePihole()
    try:
        c = fresh_core()
        r = asyncio.run(c.test_integration("pihole", {"url": srv.url}, {"password": PASSWORD}))
        assert r["ok"] and r["details"]["queries"] == 1440
        r = asyncio.run(c.test_integration("pihole", {"url": srv.url}, {"password": "wrong"}))
        assert not r["ok"] and r["key"] == "source.family.auth_failed"
    finally:
        srv.stop()


def test_the_running_source_feeds_the_core():
    srv = FakePihole()

    async def go():
        c = fresh_core()
        iid = c.save_integration("pihole", "Pi-hole", {"url": srv.url}, {})["id"]
        src = integrations.load_read("pihole", url=srv.url, password=PASSWORD, interval=30)
        task = asyncio.create_task(src.run(lambda ev: c.on_event("int:%d" % iid, iid, ev)))
        for _ in range(100):
            if iid in c.dns:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return c, iid

    try:
        c, iid = asyncio.run(go())
        assert c.dns[iid]["state"]["totals"]["blocked"] == 432 and c.source_status["int:%d" % iid]["ok"]
    finally:
        srv.stop()


# ---------------------------------------------------------------------------- the night report
def ctx_for(c, now):
    return contrib.Ctx(core=c, journal=None, since=now - 86400, until=now, prev=None)


def test_night_section_calm_and_with_points():
    c = fresh_core()
    assert not contrib._dns_available(ctx_for(c, time.time()))                 # no filter: no section
    iid = connect(c)
    now = time.time()
    c.on_event("int:%d" % iid, iid, reading_event(now))
    sec = contrib._dns(ctx_for(c, now))
    assert sec["summary"]["key"] == "report.dns.calm" and sec["level"] == "ok" and sec["items"] == []
    ev = reading_event(now)
    ev["state"]["blocking"] = False
    ev["state"]["upstream_ok"] = False
    c.on_event("int:%d" % iid, iid, ev)
    sec = contrib._dns(ctx_for(c, now))
    keys = [i["key"] for i in sec["items"]]
    assert keys == ["report.dns.finding.protection_off", "report.dns.finding.upstream_down"] and sec["level"] == "warn"
    assert all(i["point"] and i["vars"]["name"] == "Pi-hole" for i in sec["items"]) and sec["summary"]["key"] == "report.dns.summary"
    c.source_status["int:%d" % iid] = {"ok": False, "key": "source.family.unreachable", "t": now}
    sec = contrib._dns(ctx_for(c, now))
    assert sec["items"][0]["key"] == "report.dns.down" and sec["level"] == "danger"


# ---------------------------------------------------------------------------- the assistant and the catalog
def test_a_scanned_web_page_leads_to_a_suggestion_with_the_address_filled_in():
    c = fresh_core()
    c.ingest({"hosts": [{"ip": "192.0.2.2", "mac": "02:4c:4c:00:00:02", "hostnames": ["dns.lan"], "mdns": [], "ssdp": [], "sources": ["arp"],
                         "ports": [{"port": 80, "proto": "tcp", "service": "http", "banner": {"title": "Pi-hole dns.lan"}}]}]}, None)
    c._refresh_index()
    items = [s for s in catalog.suggestions(c) if s["kind"] == "pihole"]
    assert len(items) == 1
    it = items[0]
    assert it["entry"] == "dns_filter" and it["status"] == "available" and it["mode"] == "read" and it["guide"] == "dns"
    assert it["prefill"] == {"url": "http://192.0.2.2"} and it["guide_prefix"] == "setup.guide.dns" and it["steps"] == 4
    connect(c, "http://192.0.2.2")
    assert [s for s in catalog.suggestions(c) if s["kind"] == "pihole"][0]["configured"] is True


def test_the_catalog_has_a_dns_filter_class_that_is_available():
    cls = {x["id"]: x for x in catalog.classes()}["dns_filter"]
    assert cls["status"] == "available" and cls["source"] == "family"
    assert {m.family for m in integrations.BUILTIN if m.catalog == "dns_filter"} == set(integrations.families_reading("dns"))
    assert len(integrations.families_reading("dns")) == 2 and "pihole" in integrations.families_reading("dns")


# ---------------------------------------------------------------------------- the demo
def make_app(tmp_path, monkeypatch, demo_seed=False):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    if demo_seed:
        monkeypatch.setenv("LANLOOM_DEMO", "1")
        monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    return create_app(config_mod.load())


def test_the_demo_has_a_filter_with_history_and_a_finding(tmp_path, monkeypatch):
    application = make_app(tmp_path, monkeypatch, demo_seed=True)
    with TestClient(application, base_url="http://lanloom.test"):
        c = application.state.ll.core
        fam = demo._demo_family("dns")
        iid = next(i["id"] for i in c.integrations() if i["kind"] == fam)
        assert c.dns[iid]["state"]["totals"]["queries"] > 1000 and len(c.dns_log.days(iid, 35)) == 35
        assert sum(1 for d in c.dns_log.days(iid, 35) if d[1] > 0) >= 30
        v = dnsfilter.filter_view(c, iid)
        assert [f["key"] for f in v["findings"]] == ["dns.finding.share_high"]
        assert "dns:%d" % iid in {x["id"] for x in mapmodel.build(c, "overview")["components"]}
        assert any(cl["device"] for cl in v["top_clients"])
        assert demo_dns.FILTER_URL.startswith("http://192.168.50.")


# ---------------------------------------------------------------------------- the routes
def sign_in(client, data_dir):
    code = (Path(data_dir) / "setup-code.txt").read_text().strip()
    r = client.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=HDR)
    assert r.status_code == 200


def test_routes_answer_totals_only(tmp_path, monkeypatch):
    application = make_app(tmp_path, monkeypatch)
    with TestClient(application, base_url="http://lanloom.test") as client:
        sign_in(client, tmp_path)
        core = application.state.ll.core
        assert client.get("/api/dns", headers=HDR).json() == {"filters": []}
        iid = core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES('pihole','Pi-hole',?,0,0)",
                                 (json.dumps({"url": "http://192.0.2.2"}),)).lastrowid
        core.on_event("int:%d" % iid, iid, reading_event())
        core._refresh_index()
        filters = client.get("/api/dns", headers=HDR).json()["filters"]
        assert len(filters) == 1 and filters[0]["component"] == "dns:%d" % iid and len(filters[0]["hours"]) == 24
        assert filters[0]["queries"] == 1440 and filters[0]["share"] == 0.3
        one = client.get("/api/dns/%d" % iid, headers=HDR).json()
        assert one["totals"]["queries"] == 1440 and one["top_blocked"] and one["lists"]
        assert client.get("/api/dns/9999", headers=HDR).status_code == 404
        assert "password" not in json.dumps(one).lower()
        assert client.get("/api/state", headers=HDR).json()["dns"] == 1
        card = client.get("/api/component/dns:%d" % iid, headers=HDR).json()
        assert card["dns"][0]["integ"] == iid
