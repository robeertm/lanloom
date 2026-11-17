"""Inventory and life cycle (L1): own fields, states with a fixed clock, the "probably removed" heuristic, provisional entries,
decommissioning, the end-of-updates table, bulk editing, filters, the nightly section and the HTTP rules per role.

Every time-dependent rule is tested with a fixed clock (NOW), never the wall clock. Names and addresses are sample data."""
import datetime
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import core as core_mod
from lanloom import eol as eol_mod
from lanloom import inventory as inv_mod
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.store import Store

DAY = 86400.0
NOW = datetime.datetime(2026, 11, 17, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
TODAY = datetime.date(2026, 11, 17)


def plus(days: int) -> str:
    return (TODAY + datetime.timedelta(days=days)).isoformat()


@pytest.fixture()
def core(tmp_path):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    c = core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))
    c.inventory.clock = lambda: NOW
    store.execute("INSERT INTO scans(started, finished, mode, cidrs, status, hosts) VALUES(?,?,?,?,?,?)",
                  (NOW - 4000, NOW - 3600, "gentle", "[]", "done", 0))
    return c


def add(c, n, title="", kind="computer", ago=0.0, first_ago=40.0, data=None, mac=None, vendor=None):
    """A device last seen ``ago`` days before NOW and first seen ``first_ago`` days before NOW."""
    did = "d-test-%d" % n
    d = dict(data or {})
    c.store.upsert_device({"id": did, "ip": "192.168.50.%d" % n, "mac": mac, "data": d, "kind": kind, "confidence": 0.9,
                           "title": title or "Device %d" % n}, None)
    c.store.execute("UPDATE devices SET first_seen=?, last_seen=?, last_scan=NULL WHERE id=?", (NOW - first_ago * DAY, NOW - ago * DAY, did))
    c._refresh_index()
    return did


def item(c, did):
    return c.inventory.get(did, NOW)


# ------------------------------------------------------------------ own fields
def test_fields_are_cleaned_and_validated(core):
    a = add(core, 1)
    r = core.inventory.set_fields(a, {"purpose": "  Core\x00 switch  ", "room": "Hall", "note": "x" * 900, "serial": "AB-12<script>34",
                                      "purchase_date": "2024-02-29", "warranty_until": plus(100)})
    assert r["purpose"] == "Core switch" and len(r["note"]) == 400 and r["serial"] == "AB-12script34"
    assert r["confirmed"] is True and r["provisional"] is False
    for bad, why in (({"purchase_date": "2024-13-01"}, "inventory.err.date"), ({"warranty_until": "tomorrow"}, "inventory.err.date"),
                     ({"purchase_date": plus(30)}, "inventory.err.future"), ({"colour": "red"}, "inventory.err.field"),
                     ({"owner": "nobody"}, "inventory.err.owner"), ({"purchase_date": plus(-10), "warranty_until": plus(-20)}, "inventory.err.order")):
        with pytest.raises(ValueError) as exc:
            core.inventory.set_fields(a, bad)
        assert str(exc.value) == why
    assert core.inventory.set_fields("d-nope", {"room": "x"}) is None
    assert item(core, a)["room"] == "Hall"           # a refused change changes nothing


def test_owner_must_be_an_account(core):
    a = add(core, 1)
    core.store.execute("INSERT INTO users(username, pw_hash, user_handle, created, role) VALUES('Anna', 'x', x'00', 0, 'viewer')")
    assert core.inventory.set_fields(a, {"owner": "anna"})["owner"] == "Anna"       # stored the way the account is written
    core.store.execute("UPDATE users SET disabled=1")
    with pytest.raises(ValueError):
        core.inventory.set_fields(a, {"owner": "Anna"})


def test_serial_reported_by_the_device_is_shown_beside_the_own_one(core):
    a = add(core, 1, data={"ssdp": [{"st": "upnp:rootdevice", "serial": "SN-0042 <b>", "model": "X"}]})
    it = item(core, a)
    assert it["serial_reported"] == "SN-0042 b" and it["serial"] == ""


def test_device_id_change_moves_the_entry(core):
    a = add(core, 1)
    core.inventory.set_fields(a, {"room": "Attic"})
    core.inventory.rename_device(a, "d-new")
    assert core.inventory.record("d-new")["room"] == "Attic" and core.inventory.record(a)["room"] == ""
    core.inventory.forget("d-new")
    assert core.inventory.record("d-new")["room"] == ""


# ------------------------------------------------------------------ states
def test_states_follow_the_days_without_a_sign_of_life(core):
    fresh, six, seven, thirty = (add(core, 1, ago=0.1), add(core, 2, ago=6.5), add(core, 3, ago=7.2), add(core, 4, ago=31, first_ago=300))
    assert item(core, fresh)["state"] == "active" and item(core, six)["state"] == "active"
    missing = item(core, seven)
    assert missing["state"] == "missing" and missing["missing_days"] == 7 and missing["hints"]["score"] < 0.5
    assert item(core, thirty)["state"] == "removed" and "long_absent" in item(core, thirty)["hints"]["reasons"]


def test_nothing_is_judged_when_lanloom_did_not_look(core):
    a = add(core, 1, ago=20)
    core.store.execute("DELETE FROM scans")
    assert item(core, a)["state"] == "active" and core.inventory.list({}, NOW)["observing"] is False
    core.presence[a] = NOW - 100                         # a device answered a minute ago: Lanloom is looking
    assert core.inventory.list({}, NOW)["observing"] is True


def test_thresholds_are_settings(core):
    a = add(core, 1, ago=3.2)
    assert item(core, a)["state"] == "active"
    core.inventory.update_settings({"missing_days": 3})
    assert item(core, a)["state"] == "missing"
    for bad in ({"missing_days": 0}, {"removed_days": 3}, {"auto_remove_days": 2}, {"eol_warn_days": "x"}, {"missing_days": True}):
        with pytest.raises(ValueError):
            core.inventory.update_settings(bad)
    assert core.inventory.update_settings({"auto_remove_days": 0})["auto_remove_days"] == 0


def test_removal_hints_and_reasons(core):
    # an always-on kind that ran for months and a successor of the same kind and maker that appeared right after
    old = add(core, 1, "Old AP", kind="ap", ago=9, first_ago=200, data={"ssdp": [{"manufacturer": "Example Networks", "model": "AP-1"}]})
    new = add(core, 2, "New AP", kind="ap", ago=0.1, first_ago=8.8, data={"ssdp": [{"manufacturer": "Example Networks", "model": "AP-2"}]})
    it = item(core, old)
    assert it["state"] == "removed" and set(it["hints"]["reasons"]) == {"successor", "always_on"} and it["hints"]["score"] == 0.55
    assert it["hints"]["successor"]["id"] == new
    # another maker: no successor, an always-on kind alone is not enough
    core.store.execute("UPDATE devices SET data=? WHERE id=?", (json.dumps({"ssdp": [{"manufacturer": "Other Co", "model": "AP-2"}]}), new))
    core._refresh_index()
    it = item(core, old)
    assert it["state"] == "missing" and it["hints"]["reasons"] == ["always_on"]


def test_keep_dismisses_the_suggestion_until_the_device_shows_up_again(core):
    a = add(core, 1, ago=45, first_ago=300)
    assert item(core, a)["state"] == "removed"
    r = core.inventory.lifecycle(a, "keep")
    assert r["state"] == "missing" and r["confirmed"] is True
    core.store.execute("UPDATE devices SET last_seen=? WHERE id=?", (NOW - 40 * DAY, a))   # seen again, then gone again
    assert item(core, a)["state"] == "removed"


def test_port_reuse_is_a_reason(core):
    a = add(core, 1, "Old", kind="iot", ago=9, first_ago=100)
    b = add(core, 2, "New", kind="iot", ago=0.1, first_ago=100)
    core.topology = lambda: {"links": {a: {"parent": "d-sw", "port": "7"}, b: {"parent": "d-sw", "port": "7"}}}
    core.presence[b] = NOW
    it = item(core, a)
    assert "port_reused" in it["hints"]["reasons"] and it["hints"]["score"] == 0.3 and it["state"] == "missing"


# ------------------------------------------------------------------ provisional entries
def test_automatic_entries_vanish_after_seven_days_unless_confirmed(core):
    auto = add(core, 1, "Guest phone", kind="phone", ago=8)
    confirmed = add(core, 2, "Old camera", kind="camera", ago=8)
    renamed = add(core, 3, "Tablet", kind="phone", ago=8)
    gateway = add(core, 4, "Router", kind="router", ago=8, data={"is_gateway": True})
    recent = add(core, 5, "Phone", kind="phone", ago=3)
    core.inventory.lifecycle(confirmed, "confirm")
    core.patch_device(renamed, {"title": "Kitchen tablet"})
    assert item(core, auto)["provisional"] and not item(core, confirmed)["provisional"] and not item(core, renamed)["provisional"]
    assert item(core, gateway)["protected"]
    res = core.inventory.housekeeping(NOW)
    assert [r["id"] for r in res["removed"]] == [auto]
    ids = {d["id"] for d in core.store.devices()}
    assert auto not in ids and {confirmed, renamed, gateway, recent} <= ids
    ev = core.reports.journal.between(NOW - 10, NOW + 10, ("inv_expired",))
    assert len(ev) == 1 and ev[0]["data"]["title"] == "Guest phone"
    assert core.inventory.housekeeping(NOW)["removed"] == []


def test_auto_removal_can_be_switched_off_and_needs_observation(core):
    a = add(core, 1, ago=9, kind="phone")
    core.inventory.update_settings({"auto_remove_days": 0})
    assert core.inventory.housekeeping(NOW)["removed"] == []
    core.inventory.update_settings({"auto_remove_days": 7})
    core.store.execute("DELETE FROM scans")
    assert core.inventory.housekeeping(NOW)["removed"] == []          # no observation, no judgement
    assert core.store.device(a)


def test_signs_of_life_survive_a_restart(core):
    a = add(core, 1, ago=9, kind="phone")
    core.presence[a] = NOW - 3600
    core.inventory.flush_alive(NOW)
    core.presence.clear()                                              # the process restarted
    assert item(core, a)["state"] == "active" and abs(item(core, a)["alive_at"] - (NOW - 3600)) < 1
    assert core.inventory.housekeeping(NOW)["removed"] == []


# ------------------------------------------------------------------ decommissioning
def test_decommission_hides_and_reactivate_restores(core):
    old = add(core, 1, "Old NAS", kind="nas", ago=1)
    new = add(core, 2, "New NAS", kind="nas", ago=0)
    r = core.inventory.lifecycle(old, "decommission", at=plus(-1), replaced_by=new, note="disks worn out")
    assert r["state"] == "decommissioned" and r["decommissioned"]["replaced_by"] == {"id": new, "title": "New NAS"}
    assert core.store.device(old)["hidden"] == 1
    assert old not in [d["id"] for d in core.devices() if not d["hidden"]]
    assert core.inventory.list({"state": "open"}, NOW)["summary"]["decommissioned"] == 1
    assert [i["id"] for i in core.inventory.list({"state": "open"}, NOW)["items"]] == [new]
    r = core.inventory.lifecycle(old, "reactivate")
    assert r["state"] == "active" and core.store.device(old)["hidden"] == 0
    for bad in (("decommission", {"at": "2026-02-30"}), ("decommission", {"at": plus(30)}), ("decommission", {"replaced_by": old}),
                ("decommission", {"replaced_by": "d-none"}), ("reactivate", {}), ("explode", {})):
        with pytest.raises(ValueError):
            core.inventory.lifecycle(old, bad[0], **bad[1])


def test_a_hidden_device_stays_hidden_after_reactivation_if_the_person_hid_it(core):
    a = add(core, 1, ago=1)
    core.store.execute("UPDATE devices SET hidden=1 WHERE id=?", (a,))
    core.inventory.lifecycle(a, "decommission")
    core.inventory.lifecycle(a, "reactivate")
    assert core.store.device(a)["hidden"] == 1


def test_a_retired_device_that_answers_again_is_flagged(core):
    a = add(core, 1, ago=0.2, kind="nas")
    core.inventory.lifecycle(a, "decommission", at=plus(-10))
    assert item(core, a)["decommissioned"]["seen_again"] is True
    b = add(core, 2, ago=20, kind="nas")
    core.inventory.lifecycle(b, "decommission", at=plus(-10))
    assert item(core, b)["decommissioned"]["seen_again"] is False


# ------------------------------------------------------------------ warranty and end of updates
def test_warranty_states(core):
    a, b, c_ = add(core, 1), add(core, 2), add(core, 3)
    core.inventory.set_fields(a, {"warranty_until": plus(40)})
    core.inventory.set_fields(b, {"warranty_until": plus(30)})
    core.inventory.set_fields(c_, {"warranty_until": plus(-1)})
    assert item(core, a)["warranty"] == {"until": plus(40), "days_left": 40, "state": "ok"}
    assert item(core, b)["warranty"]["state"] == "soon" and item(core, c_)["warranty"]["state"] == "expired"
    s = core.inventory.list({}, NOW)["summary"]
    assert s["warranty_soon"] == 1 and s["warranty_expired"] == 1


FAKE_TABLE = [{"id": "ex-os-20", "match": [r"\bexample os 20\b"], "until": "2025-05-31"},
              {"id": "ex-os-10", "match": [r"\bexample os 10\b"], "until": "2025-10-14"}]


def test_end_of_updates_from_the_table_and_the_own_date_wins(core):
    core.inventory.eol = eol_mod.EolTable.from_raw(FAKE_TABLE, as_of="2026-01-01")      # fictional products only
    a = add(core, 1, "Server", kind="server", data={"snmp": {"descr": "Managed server, Example OS 20 LTS"}})
    e = item(core, a)["eol"]
    assert e["id"] == "ex-os-20" and e["source"] == "table" and e["state"] == "past" and e["name"] is None
    core.inventory.set_fields(a, {"eol_until": plus(100)})
    e = item(core, a)["eol"]
    assert e["source"] == "own" and e["state"] == "soon" and e["days_left"] == 100
    core.inventory.set_fields(a, {"eol_until": plus(400)})
    assert item(core, a)["eol"]["state"] == "ok"
    core.inventory.set_fields(a, {"eol_until": ""})
    assert item(core, a)["eol"]["source"] == "table"
    # the firmware the person writes down counts as well
    b = add(core, 2, "PC", kind="computer")
    assert item(core, b)["eol"] is None
    core.inventory.set_fields(b, {"firmware": "Example OS 10 Pro 22H2"})
    assert item(core, b)["eol"]["id"] == "ex-os-10" and item(core, b)["firmware_source"] == "own"


def test_firmware_from_a_family_and_from_the_hub(core):
    a = add(core, 1, "Switch", kind="switch", data={"snmp": {"descr": "Managed switch, firmware 2.4.1"}})
    assert item(core, a)["firmware"] == "2.4.1" and item(core, a)["firmware_source"] == "snmp"
    core.integrations = lambda: [{"id": 5, "name": "Controller"}]
    core.live_cfg = {5: {"t": time.time(), "state": {"equipment": [{"mgmt_ip": "192.168.50.99", "firmware": "7.1.3"}]}}}
    assert item(core, a)["firmware_source"] == "snmp"                       # no device has that management address
    core.live_cfg[5]["state"]["equipment"][0]["mgmt_ip"] = "192.168.50.1"
    assert (item(core, a)["firmware"], item(core, a)["firmware_source"]) == ("7.1.3", "family")
    core.live_cfg = {}
    b = add(core, 2, "Plug", kind="iot")
    core.hub = {1: {"snapshot": {"devices": [{"id": "hd1", "sw": "3.2.1"}]}}}
    core.hub_merge = lambda devices=None: {"by_net": {b: [{"integ": 1, "device_id": "hd1"}]}}
    assert (item(core, b)["firmware"], item(core, b)["firmware_source"]) == ("3.2.1", "hub")
    core.inventory.set_fields(b, {"firmware": "9.9"})
    assert (item(core, b)["firmware"], item(core, b)["firmware_source"]) == ("9.9", "own")


# ------------------------------------------------------------------ the table file
def test_the_bundled_table_is_valid():
    as_of, entries = eol_mod.load_table(eol_mod.DATA_FILE)
    doc = json.loads(eol_mod.DATA_FILE.read_text())
    assert as_of and len(entries) == len(doc["entries"]) >= 10
    assert len({e["id"] for e in entries}) == len(entries)
    t = eol_mod.EolTable()
    for e in entries:                     # every entry carries a sample its own patterns match - and no other entry's patterns decide it
        assert e["sample"], e["id"]
        assert t.lookup(e["sample"])["id"] == e["id"], e["id"]
        assert t.lookup(e["sample"])["name"] is None            # the bundled table shows no product names
    assert t.lookup("") is None and t.lookup(None, None) is None and t.lookup("example os 99") is None


def test_a_bad_entry_is_skipped_and_an_own_table_overrides(tmp_path):
    good = {"id": "x1", "name": "X", "match": ["foo"], "until": "2030-01-01"}
    bad = [{"id": "Bad Id", "name": "X", "match": ["a"], "until": "2030-01-01"}, {"id": "b2", "name": "X", "match": ["("], "until": "2030-01-01"},
           {"id": "b3", "name": "X", "match": ["a"], "until": "2030-02-31"}, {"id": "b4", "name": 5, "match": ["a"], "until": "2030-01-01"},
           {"id": "b5", "name": "X", "match": [], "until": "2030-01-01"}, "nonsense"]
    p = tmp_path / "eol.json"
    p.write_text(json.dumps({"as_of": "2027-01-01", "entries": bad + [good]}))
    assert [e["id"] for e in eol_mod.load_table(p)[1]] == ["x1"]
    p.write_text("not json")
    assert eol_mod.load_table(p) == (None, [])
    own = tmp_path / "own.json"
    bundled_id = eol_mod.EolTable().entries[0]["id"]
    own.write_text(json.dumps({"as_of": "2099-01-01", "entries": [{"id": bundled_id, "name": "Mine", "match": ["my own product"], "until": "2031-01-01"}]}))
    t = eol_mod.EolTable(extra=own)
    assert t.lookup("my own product 3")["until"] == "2031-01-01" and t.lookup("my own product")["name"] == "Mine" and t.as_of == "2099-01-01"


# ------------------------------------------------------------------ list, filter, sort, bulk
def test_filter_sort_and_search(core):
    a, b, c_ = add(core, 1, "Alpha"), add(core, 2, "Bravo", kind="printer"), add(core, 3, "Charlie", ago=12, first_ago=200)
    core.inventory.set_fields(a, {"room": "Office", "purpose": "Laptop", "warranty_until": plus(5)})
    core.inventory.set_fields(b, {"room": "Hall", "purpose": "Printer for the office", "warranty_until": plus(-5)})
    L = lambda **q: [i["title"] for i in core.inventory.list(q, NOW)["items"]]
    assert L() == ["Alpha", "Bravo", "Charlie"]
    assert L(sort="room") == ["Bravo", "Alpha", "Charlie"]                 # empty values last
    assert L(sort="room", dir="desc") == ["Alpha", "Bravo", "Charlie"]
    assert L(state="missing") == ["Charlie"] and L(warranty="soon") == ["Alpha"] and L(warranty="expired") == ["Bravo"] and L(warranty="none") == ["Charlie"]
    assert L(room="office") == ["Alpha"] and L(kind="printer") == ["Bravo"] and L(q="office printer") == ["Bravo"] and L(q="zzz") == []
    assert L(state="attention") == ["Alpha", "Bravo", "Charlie"] and L(provisional="1") == ["Charlie"]
    facets = core.inventory.list({}, NOW)["facets"]
    assert facets["rooms"] == ["Hall", "Office"]
    assert L(sort="nonsense") == ["Alpha", "Bravo", "Charlie"]


def test_bulk_sets_only_own_fields_for_many(core):
    ids = [add(core, n) for n in (1, 2, 3)]
    r = core.inventory.bulk(ids + ["d-none"], {"room": "Rack", "purpose": "Servers"}, confirm=True)
    assert r == {"changed": 3, "skipped": 1}
    assert all(item(core, i)["room"] == "Rack" and item(core, i)["confirmed"] for i in ids)
    for bad in ({"title": "x"}, {"serial": "A1"}, {"kind": "nas"}, {"eol_until": plus(3)}):
        with pytest.raises(ValueError):
            core.inventory.bulk(ids, bad)
    with pytest.raises(ValueError):
        core.inventory.bulk([], {"room": "x"})
    with pytest.raises(ValueError):
        core.inventory.bulk(["d-%d" % i for i in range(501)], {"room": "x"})
    with pytest.raises(ValueError):
        core.inventory.bulk(ids, {})                                         # nothing to do
    assert core.inventory.bulk(ids, {}, confirm=True)["changed"] == 3
    # a warranty that ends before the purchase date is skipped for that device, the others are still changed
    core.inventory.set_fields(ids[0], {"purchase_date": plus(-5)})
    r = core.inventory.bulk(ids, {"warranty_until": plus(-10)})
    assert r == {"changed": 2, "skipped": 1}


def test_a_merged_interface_is_not_a_row(core):
    a = add(core, 1, "Main", mac="aa:bb:cc:00:00:01")
    rows = core.inventory.list({}, NOW)["items"]
    assert [r["id"] for r in rows] == [a]


# ------------------------------------------------------------------ nightly section (fixed time ranges)
def _ctx(core, since, until):
    return SimpleNamespace(core=core, since=since, until=until, events=lambda *kinds: core.reports.journal.between(since, until, kinds or None))


def test_nightly_items_appear_once_when_a_threshold_is_crossed(core):
    from lanloom import inventory_report as rep
    a = add(core, 1, "Storage")
    core.inventory.set_fields(a, {"warranty_until": plus(30)})
    # the 30-day mark of that warranty lies at the start of today (UTC): only the window around it reports it
    midnight = datetime.datetime(2026, 11, 17, tzinfo=datetime.timezone.utc).timestamp()
    items = rep._build_items(_ctx(core, midnight - 3600, midnight + 3600))
    assert [(i["key"], i["vars"]["days"]) for i in items] == [("inventory.report.warranty_soon", 30)]
    assert rep._build_items(_ctx(core, midnight + 3600, midnight + 90000)) == []
    core.inventory.set_fields(a, {"warranty_until": plus(-1)})
    items = rep._build_items(_ctx(core, midnight - 3600, midnight + 3600))
    assert [i["key"] for i in items] == ["inventory.report.warranty_over"]


def test_nightly_reports_missing_removed_and_expired_entries(core):
    from lanloom import inventory_report as rep
    miss = add(core, 1, "Camera", kind="camera", ago=0, first_ago=3)
    gone = add(core, 2, "Old AP", kind="ap", ago=0, first_ago=300)
    for did, ago_s in ((miss, 7 * DAY + 3600), (gone, 30 * DAY + 3600)):
        core.store.execute("UPDATE devices SET last_seen=? WHERE id=?", (NOW - ago_s, did))
        core.inventory.lifecycle(did, "confirm")
    core.reports.journal.add("inv_expired", {"id": "d-x", "title": "Guest", "kind": "phone", "ip": "192.168.50.9", "since": NOW - 8 * DAY}, NOW - 1000)
    items = {i["id"].split(":")[0]: i for i in rep._build_items(_ctx(core, NOW - 7200, NOW))}
    assert set(items) == {"inv-miss", "inv-gone", "inv-exp"}
    assert items["inv-miss"]["key"] == "inventory.report.missing" and items["inv-gone"]["key"] == "inventory.report.removed"
    # nothing to say before it happened
    assert rep._build_items(_ctx(core, NOW - 10 * DAY, NOW - 9 * DAY)) == []


def test_the_section_is_left_out_when_there_is_nothing_to_report(core):
    from lanloom import inventory_report as rep
    add(core, 1)
    assert rep._available(_ctx(core, NOW - 86400, NOW)) is False


# ------------------------------------------------------------------ HTTP: roles, validation, audit
@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from lanloom import config as config_mod
    from lanloom.main import create_app
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers={"X-Lanloom": "1"})
        assert r.status_code == 200
        c.st = app.state.ll
        yield c


H = {"X-Lanloom": "1"}


def test_http_roundtrip_and_audit(app_client):
    c = app_client
    data = c.get("/api/inventory").json()
    assert data["summary"]["missing"] == 1 and data["summary"]["removed"] == 1 and data["summary"]["decommissioned"] == 1 and "admin" in data["people"]
    storage = next(i for i in data["items"] if i["title"] == "Storage")
    r = c.put("/api/devices/%s/inventory" % storage["id"], json={"room": "Cellar", "note": "secret note text"}, headers=H)
    assert r.status_code == 200 and r.json()["room"] == "Cellar"
    assert c.get("/api/inventory?room=cellar").json()["items"][0]["id"] == storage["id"]
    assert c.put("/api/devices/%s/inventory" % storage["id"], json={"warranty_until": "x"}, headers=H).json() == {"error": "inventory.err.date"}
    assert c.get("/api/devices/nope/inventory").status_code == 404
    r = c.post("/api/devices/%s/lifecycle" % storage["id"], json={"action": "keep"}, headers=H)
    assert r.status_code == 200 and r.json()["confirmed"] is True
    r = c.post("/api/inventory/bulk", json={"ids": [storage["id"]], "set": {"room": "Rack"}, "confirm": True}, headers=H)
    assert r.json() == {"changed": 1, "skipped": 0}
    assert c.put("/api/inventory/settings", json={"missing_days": 9}, headers=H).json()["missing_days"] == 9
    acts = [(e["action"], e["outcome"]) for e in c.st.audit.query({}, 50)]
    for want in ("inventory.change", "inventory.lifecycle", "inventory.bulk", "inventory.settings"):
        assert (want, "ok") in acts
    # the log names fields, never their values
    assert "secret note text" not in json.dumps(c.st.audit.query({}, 50))


def test_http_roles(app_client):
    c = app_client
    st = c.st
    st.accounts.create("view", "a-long-test-password", role="viewer")
    st.accounts.create("op", "a-long-test-password", role="operator")
    did = c.get("/api/inventory").json()["items"][0]["id"]

    def as_(user):
        c.cookies.clear()
        r = c.post("/api/login", json={"username": user, "password": "a-long-test-password"}, headers=H)
        assert r.status_code == 200, r.text

    def codes():
        return (c.get("/api/inventory").status_code, c.put("/api/devices/%s/inventory" % did, json={"room": "R"}, headers=H).status_code,
                c.post("/api/devices/%s/lifecycle" % did, json={"action": "confirm"}, headers=H).status_code,
                c.post("/api/inventory/bulk", json={"ids": [did], "set": {"room": "R"}}, headers=H).status_code,
                c.get("/api/inventory/settings").status_code, c.put("/api/inventory/settings", json={"missing_days": 8}, headers=H).status_code)
    as_("view")
    assert codes() == (200, 403, 403, 403, 200, 403)
    as_("op")
    assert codes() == (200, 200, 200, 200, 200, 403)
