"""Integration catalog (what can be connected to which device) and the routes of the zone, protection and
integration views."""
import copy
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import access, auth, core, demo, integrations, setup_assist
from lanloom import config as config_mod
from lanloom.integrations import catalog
from lanloom.main import create_app
from lanloom.store import Store

HDR = {"X-Lanloom": "1"}
PRINTER_ID, CAM_ID, ADAPTER_CAM_ID = "d-024c4c000028", "d-024c4c000032", "d-00005e007010"


@pytest.fixture()
def demo_core():
    st = Store(":memory:")
    c = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=True), st, None)
    c.demo = demo.Demo()
    c.demo.seed(c)
    return c


def _raw():
    return json.loads(catalog.PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- the file
def test_catalog_file_is_valid_and_loads():
    classes = catalog._validate(_raw())
    assert [c["id"] for c in catalog.classes()] == [c["id"] for c in classes]
    assert len({c["id"] for c in classes}) == len(classes)
    assert catalog.classes() is not catalog.classes() and catalog.classes()[0] is not catalog.classes()[0]   # copies


def test_every_selectable_family_and_every_builtin_source_has_a_class():
    by_id = {c["id"]: c for c in catalog.classes()}
    for kind in ("hub", "mqtt", "snmp"):
        assert by_id[kind]["source"] == "builtin" and by_id[kind]["kind"] == kind and by_id[kind]["status"] == "available"
        assert kind in setup_assist.GUIDE_STEPS
    fams = integrations.families(selectable_only=True)
    assert fams
    for fam in fams:
        suggestion = {"kind": fam.family, "family": fam.family, "guide": setup_assist._guide_of(fam.family, fam)}
        cls = catalog._class_for(suggestion, by_id)
        assert cls is not None and cls["source"] == "family", fam.family
    assert {c["guide"] for c in by_id.values() if c["source"] == "family"} == {"family", "hv", "dns"}


def test_planned_classes_have_a_topic_and_no_connect_button():
    planned = [c for c in catalog.classes() if c["status"] == "planned"]
    assert {c["id"] for c in planned} >= {"printer", "camera", "container_host"}
    ups = next(c for c in catalog.classes() if c["id"] == "ups")
    assert ups["status"] == "available" and ups["kind"] == "nut" and "topic" not in ups          # a UPS is read over its monitoring service now
    for c in planned:
        assert c["topic"] in catalog.TOPICS and "kind" not in c and c["source"] == "catalog", c["id"]
    for p in catalog.public():
        if p["status"] == "planned":
            assert p["guide"] == "catalog.%s.guide" % p["id"] and "kind" not in p and p["topic"] in catalog.TOPICS
        else:
            assert p["guide"].startswith("setup.guide.") and p["topic"] is None
    assert all(set(p) == {"id", "status", "topic", "mode", "benefit", "steps", "guide"} for p in catalog.public())


def test_available_catalog_classes_name_a_kind_the_assistant_knows():
    for c in catalog.classes():
        if c["source"] == "catalog" and c["status"] == "available":
            assert c["kind"] in setup_assist.GUIDE_STEPS and c["detect"], c["id"]


def test_catalog_has_no_maker_or_product_names():
    raw = _raw()
    text = json.dumps({k: v for k, v in raw.items() if k != "note"})
    words = set(re.findall(r"[A-Za-z][A-Za-z0-9]*", text))
    assert all(w == w.lower() for w in words), sorted(w for w in words if w != w.lower())
    assert "maker and product names stay out" in raw["note"]
    # no family of the registry may be named in the file either
    for fam in integrations.families():
        for word in re.findall(r"[a-z]{4,}", (fam.family + " " + fam.label).lower()):
            assert word not in text.lower(), word
    assert not re.search(r"[äöüß]", catalog.PATH.read_text(encoding="utf-8").lower())


@pytest.mark.parametrize("mutate,why", [
    (lambda d: d.update(version=2), "catalog shape"),
    (lambda d: d.update(classes={}), "catalog shape"),
    (lambda d: d["classes"].append(dict(d["classes"][0])), "duplicate"),
    (lambda d: d["classes"][0].update(extra=1), "class"),
    (lambda d: d["classes"][0].update(id="not an id"), "class"),
    (lambda d: d["classes"][0].update(status="soon"), "source/status/mode"),
    (lambda d: d["classes"][0].update(mode="write"), "source/status/mode"),
    (lambda d: d["classes"][0].update(source="plugin"), "source/status/mode"),
    (lambda d: d["classes"][0].update(benefit=101), "benefit/steps"),
    (lambda d: d["classes"][0].update(steps=0), "benefit/steps"),
    (lambda d: d["classes"][0].update(benefit=True), "benefit/steps"),
    (lambda d: next(c for c in d["classes"] if c["id"] == "printer").pop("topic"), "planned needs a known topic"),
    (lambda d: next(c for c in d["classes"] if c["id"] == "printer").update(topic="weather"), "planned needs a known topic"),
    (lambda d: next(c for c in d["classes"] if c["id"] == "ups").update(detect=[]), "no signs"),
    (lambda d: next(c for c in d["classes"] if c["id"] == "ups").update(detect=[{"port": 1, "brand": "x"}]), "sign"),
    (lambda d: next(c for c in d["classes"] if c["id"] == "nas").update(kind="toaster"), "available class names a kind"),
])
def test_validation_refuses_a_broken_catalog(mutate, why):
    data = copy.deepcopy(_raw())
    mutate(data)
    with pytest.raises(catalog.CatalogError) as e:
        catalog._validate(data)
    assert why in str(e.value)


# ---------------------------------------------------------------------- suggestions
def _by_entry(items):
    out = {}
    for it in items:
        out.setdefault(it["entry"], []).append(it)
    return out


def test_demo_suggestions_cover_connections_and_planned_classes(demo_core):
    items = catalog.suggestions(demo_core)
    e = _by_entry(items)
    assert {"snmp", "hub", "mqtt", "camera", "printer"} <= set(e)
    assert {i["title"] for i in e["snmp"]} >= {"Router", "Core switch"}
    cams = {i["device"] for i in e["camera"]}
    assert {CAM_ID, "d-024c4c000033", ADAPTER_CAM_ID, "d-00005e007030"} <= cams        # also the cameras only an adapter knows
    assert all(i["status"] == "planned" and i["topic"] == "cameras" for i in e["camera"])
    assert [i["device"] for i in e["printer"]] == [PRINTER_ID] and e["printer"][0]["status"] == "planned"
    assert e["printer"][0]["topic"] == "printing" and e["printer"][0]["guide_prefix"] == "catalog.printer.guide"
    assert e["hub"][0]["status"] == "available" and e["hub"][0]["guide_prefix"] == "setup.guide.hub"
    assert e["hub"][0]["prefill"]["url"].startswith("http://192.168.50.")


def test_planned_entries_are_never_marked_configured_and_carry_no_prefill_for_a_button(demo_core):
    for it in catalog.suggestions(demo_core):
        if it["status"] == "planned":
            assert it["configured"] is False and it["prefill"] == {} and it["kind"] == it["entry"], it["entry"]
        assert it["certainty"] in ("sure", "probable") and it["reasons"]
        assert set(it) >= {"entry", "status", "mode", "benefit", "topic", "guide_prefix", "steps", "device", "ip", "title"}


def test_one_entry_per_class_and_device(demo_core):
    items = catalog.suggestions(demo_core)
    keys = [(i["entry"], i["device"]) for i in items]
    assert len(keys) == len(set(keys))
    assert len(items) <= 200


def test_sorted_not_connected_first_then_available_then_benefit(demo_core):
    items = catalog.suggestions(demo_core)
    flags = [(i["configured"], i["status"] != "available") for i in items]
    assert flags == sorted(flags)
    first_planned = next(n for n, i in enumerate(items) if i["status"] == "planned")
    assert all(i["status"] == "available" for i in items[:first_planned])
    avail = [i["benefit"] for i in items if i["status"] == "available"]
    assert avail == sorted(avail, reverse=True) or avail[0] == max(avail)
    assert items[0]["entry"] == "snmp" and items[0]["benefit"] == 100


def test_connected_entries_go_last_and_are_counted(demo_core):
    before = catalog.suggestions(demo_core)
    assert not any(i["configured"] for i in before)
    demo_core.store.execute("INSERT INTO integrations(kind, name, config, enabled, created) VALUES('hub','Hub',?,1,?)",
                            (json.dumps({"url": "http://192.168.50.14:8123"}), time.time()))
    after = catalog.suggestions(demo_core)
    flags = [i["configured"] for i in after]
    assert flags == sorted(flags) and flags.count(True) == 1
    done = next(i for i in after if i["configured"])
    assert done["entry"] == "hub" and done["ip"] == "192.168.50.14"
    n = catalog.counts(after)
    assert n["connected"] == 1 and n["planned"] == sum(1 for i in after if i["status"] == "planned")
    assert n["available"] == sum(1 for i in after if i["status"] == "available") - 1


def test_for_device_filters_by_device(demo_core):
    mine = catalog.for_device(demo_core, PRINTER_ID)
    assert [i["entry"] for i in mine] == ["printer"] and all(i["device"] == PRINTER_ID for i in mine)
    assert {i["entry"] for i in catalog.for_device(demo_core, ADAPTER_CAM_ID)} == {"camera"}
    assert catalog.for_device(demo_core, "d-nothing") == []
    assert catalog.for_device(demo_core, "d-024c4c000001") and all(i["device"] == "d-024c4c000001" for i in catalog.for_device(demo_core, "d-024c4c000001"))


def test_counts():
    items = [{"status": "available", "configured": False}, {"status": "available", "configured": True},
             {"status": "planned", "configured": False}, {"status": "planned", "configured": False}]
    assert catalog.counts(items) == {"available": 1, "connected": 1, "planned": 2}
    assert catalog.counts([]) == {"available": 0, "connected": 0, "planned": 0}


def test_hidden_and_merged_devices_get_no_catalog_entry(demo_core):
    demo_core.store.execute("UPDATE devices SET hidden=1 WHERE id=?", (PRINTER_ID,))
    demo_core._refresh_index()
    assert catalog.for_device(demo_core, PRINTER_ID) == []


# ---------------------------------------------------------------------- the routes
@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        c.st = st
        c.uids = {role: st.accounts.create("user-" + role, "a-long-test-password", role=role) for role in access.ROLES}
        yield c


def req(c, method, path, role, body=None):
    token = c.st.sessions.create(c.uids[role], ip="127.0.0.1", agent="test")
    c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, token)
    try:
        return c.request(method, path, headers=HDR, json=body if body is not None else {})
    finally:
        c.cookies.clear()


def test_rights_table_for_the_new_routes():
    rule = access.RULES
    assert rule[("GET", "/api/zones")].role == rule[("GET", "/api/protect")].role == "viewer"
    assert rule[("GET", "/api/integrate")].role == rule[("GET", "/api/devices/{device_id}/integrate")].role == "viewer"
    assert rule[("POST", "/api/devices/{device_id}/protect")].role == "operator" and rule[("POST", "/api/devices/{device_id}/protect")].audit == "device.protect"
    assert rule[("POST", "/api/zones/consent")].role == "admin" and rule[("POST", "/api/zones/consent")].audit == "zone.consent"
    assert {"device.protect", "zone.consent"} <= access.AUDIT_ACTIONS


def test_viewer_reads_zones_protection_and_integration_views(app_client):
    z = req(app_client, "GET", "/api/zones", "viewer")
    assert z.status_code == 200 and set(z.json()) == {"zones", "unzoned", "no_address", "adapter_inventory"}
    assert any(e["reach"] == "scan" for e in z.json()["zones"])
    p = req(app_client, "GET", "/api/protect", "viewer")
    assert p.status_code == 200 and list(p.json()) == ["devices"]
    assert {d["id"] for d in p.json()["devices"]} == {PRINTER_ID}
    assert p.json()["devices"][0]["class"] == "printer" and set(p.json()["devices"][0]) == {"id", "title", "ip", "mac", "kind", "class", "why", "source"}
    i = req(app_client, "GET", "/api/integrate", "viewer")
    assert i.status_code == 200 and set(i.json()) == {"items", "counts", "classes"}
    assert set(i.json()["counts"]) == {"available", "connected", "planned"} and i.json()["items"]
    assert {c["id"] for c in i.json()["classes"]} == {c["id"] for c in catalog.classes()}
    d = req(app_client, "GET", "/api/devices/%s/integrate" % PRINTER_ID, "viewer")
    assert d.status_code == 200 and [x["entry"] for x in d.json()["items"]] == ["printer"]
    assert req(app_client, "GET", "/api/devices/d-nothing/integrate", "viewer").json() == {"items": []}


def test_operator_marks_a_device_and_the_scanner_will_know(app_client):
    core_ = app_client.st.core
    r = req(app_client, "POST", "/api/devices/%s/protect" % CAM_ID, "operator", {"mark": "passive"})
    assert r.status_code == 200 and r.json()["protect"]["class"] == "passive" and r.json()["protect"]["source"] == "mark"
    assert r.json()["protect"]["mark"] == "passive"
    assert core_.protected_map()["192.168.50.50"] == {"class": "passive", "why": "protect.why.mark", "source": "mark"}
    assert {d["id"] for d in req(app_client, "GET", "/api/protect", "viewer").json()["devices"]} == {PRINTER_ID, CAM_ID}
    rows = [e for e in app_client.st.audit.query({}, 50) if e["action"] == "device.protect"]
    assert rows and rows[0]["actor"] == "user-operator" and rows[0]["outcome"] == "ok" and rows[0]["target"] == CAM_ID
    assert app_client.st.audit.verify()["ok"]
    # "none" switches the automatic class of the printer off, null forgets every mark
    r = req(app_client, "POST", "/api/devices/%s/protect" % PRINTER_ID, "operator", {"mark": "none"})
    assert r.json()["protect"]["class"] is None and r.json()["protect"]["mark"] == "none"
    assert core_.protected_map()["192.168.50.40"] == {"source": "none"}
    assert {d["id"] for d in req(app_client, "GET", "/api/protect", "viewer").json()["devices"]} == {CAM_ID}
    r = req(app_client, "POST", "/api/devices/%s/protect" % PRINTER_ID, "operator", {"mark": None})
    assert r.json()["protect"]["class"] == "printer" and r.json()["protect"]["source"] == "auto"
    r = req(app_client, "POST", "/api/devices/%s/protect" % CAM_ID, "operator", {"mark": None})
    assert r.json()["protect"] is None and "192.168.50.50" not in core_.protected_map()


def test_protect_mark_is_checked_and_needs_an_operator(app_client):
    path = "/api/devices/%s/protect" % CAM_ID
    r = req(app_client, "POST", path, "operator", {"mark": "weird"})
    assert r.status_code == 400 and r.json() == {"error": "protect.err.invalid"}
    r = req(app_client, "POST", "/api/devices/d-nothing/protect", "operator", {"mark": "passive"})
    assert r.status_code == 404 and r.json()["error"] == "device.err.missing"
    r = req(app_client, "POST", path, "viewer", {"mark": "passive"})
    assert r.status_code == 403 and r.json()["error"] == "auth.err.role"
    assert app_client.st.core.protect_marks() == {}
    for role in ("operator", "admin"):
        assert req(app_client, "POST", path, role, {"mark": "industrial"}).status_code == 200


def test_zone_consent_is_for_admins_and_follows_the_zone_list(app_client):
    zones_ = req(app_client, "GET", "/api/zones", "viewer").json()["zones"]
    behind = [z["cidr"] for z in zones_ if z["reach"] == "adapter"]
    for role in ("viewer", "operator"):
        r = req(app_client, "POST", "/api/zones/consent", role, {"cidr": "192.168.60.0/24", "on": True})
        assert r.status_code == 403 and r.json()["error"] == "auth.err.role"
    r = req(app_client, "POST", "/api/zones/consent", "admin", {"cidr": "192.168.99.0/24", "on": True})
    assert r.status_code == 400 and r.json()["error"] == "zone.err.unknown"
    r = req(app_client, "POST", "/api/zones/consent", "admin", {"cidr": "8.8.8.0/24", "on": True})
    assert r.status_code == 400 and r.json()["error"] == "scan.unsafe.public"
    r = req(app_client, "POST", "/api/zones/consent", "admin", {"on": True})
    assert r.status_code == 400 and r.json()["error"] == "scan.unsafe.invalid"
    assert app_client.st.core.zone_consent() == []
    if behind:                                                                 # the sample network may have no VLANs yet
        r = req(app_client, "POST", "/api/zones/consent", "admin", {"cidr": behind[0], "on": True})
        assert r.status_code == 200 and {z["cidr"]: z for z in r.json()["zones"]}[behind[0]]["reach"] == "consent"
        assert app_client.st.core.zone_consent() == [behind[0]]
        r = req(app_client, "POST", "/api/zones/consent", "admin", {"cidr": behind[0], "on": "yes"})   # only true switches on
        assert app_client.st.core.zone_consent() == []
    rows = [e for e in app_client.st.audit.query({}, 50) if e["action"] == "zone.consent"]
    assert any(e["outcome"] == "denied" and e["actor"] == "user-viewer" for e in rows)
    assert any(e["outcome"] == "failed" and e["actor"] == "user-admin" for e in rows)


def test_consent_on_the_core_with_a_range_that_is_known(app_client):
    """The local network is always known: consent to it is accepted and changes nothing about how it is scanned."""
    r = req(app_client, "POST", "/api/zones/consent", "admin", {"cidr": "192.168.50.0/24", "on": True})
    assert r.status_code == 200
    zone = {z["cidr"]: z for z in r.json()["zones"]}["192.168.50.0/24"]
    assert zone["reach"] == "scan"
    req(app_client, "POST", "/api/zones/consent", "admin", {"cidr": "192.168.50.0/24", "on": False})
    assert app_client.st.core.zone_consent() == []
