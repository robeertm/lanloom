"""Devices behind a repeater that rewrites hardware addresses (sample data only): the two entries of one device are
one row in the list, and an entry nothing could classify takes the class of the hub device that names its address."""
import json
from pathlib import Path

from lanloom import hubmerge
from test_hub_bridge_core import add_hub, make_core
from test_hub_merge_core import hdev, snapshot

ROOT = Path(__file__).resolve().parent.parent
REAL = "08:3a:f2:11:22:33"
TRANSLATED = "52:c7:be:11:22:33"


def put(c, dev_id, ip, mac, kind, title, data=None):
    c.store.upsert_device({"id": dev_id, "ip": ip, "mac": mac, "data": data or {}, "kind": kind, "confidence": 0.9 if kind != "unknown" else 0.0,
                           "title": title}, None)


def world(tmp_path, devices):
    c, _rep = make_core(tmp_path)
    iid = add_hub(c)
    snap = snapshot()
    snap["devices"] = list(devices)
    c.on_event("int:%d" % iid, iid, snap)
    c.structure_version += 1
    return c


def view_of(c, dev_id):
    return next(v for v in c.devices() if v["id"] == dev_id)


def test_unknown_entry_takes_the_class_of_the_hub_device_that_names_its_address(tmp_path):
    c = world(tmp_path, [hdev("plug", "Laundry", model="Smart plug", macs=[REAL])])
    put(c, "d-real", "192.168.50.53", REAL, "unknown", "Unknown device")
    v = view_of(c, "d-real")
    assert v["kind"] == "iot" and v["kind_source"] == "hub" and v["auto_kind"] == "unknown"
    assert v["reasons"][-1] == "hub:mac"
    assert c.device_view(c.store.device("d-real"))["kind"] == "iot"          # the card says the same as the list


def test_a_class_the_scan_found_or_the_user_chose_is_never_replaced(tmp_path):
    c = world(tmp_path, [hdev("plug", "Laundry", model="Plug", macs=[REAL]), hdev("tv", "Panel", model="Panel", macs=["aa:bb:cc:00:00:10"])])
    put(c, "d-real", "192.168.50.53", REAL, "computer", "Box")
    put(c, "d-tv", "192.168.50.54", "aa:bb:cc:00:00:10", "unknown", "Unknown device")
    c.store.query("UPDATE devices SET user_kind='printer' WHERE id='d-tv'")
    assert view_of(c, "d-real")["kind"] == "computer" and "kind_source" not in view_of(c, "d-real")
    assert view_of(c, "d-tv")["kind"] == "printer" and "kind_source" not in view_of(c, "d-tv")


def test_no_match_a_service_or_a_suggestion_only_leaves_it_unknown(tmp_path):
    c = world(tmp_path, [hdev("svc", "Cloud", service=True, macs=["aa:bb:cc:00:00:99"]), hdev("named", "Lone", model="Plug")])
    put(c, "d-a", "192.168.50.60", "aa:bb:cc:00:00:99", "unknown", "Lone")           # a service has no class
    put(c, "d-b", "192.168.50.61", "aa:bb:cc:00:00:98", "unknown", "Other")          # no hardware address in common
    assert view_of(c, "d-a")["kind"] == "unknown" and view_of(c, "d-b")["kind"] == "unknown"


def test_kind_of_reads_the_words_of_the_hub_device_else_smart_home():
    assert hubmerge.kind_of({"name": "Hall", "model": "Floor lamp"}) == "iot"
    assert hubmerge.kind_of({"name": "Office", "model": "Laser printer"}) == "printer"
    assert hubmerge.kind_of({"name": "X", "model": "Y"}) == "iot"
    assert hubmerge.kind_of({"name": "Cloud", "service": True}) is None
    assert hubmerge.kind_of({"id": "z"}) is None and hubmerge.kind_of(None) is None


def test_repeater_pair_is_one_device_in_the_core(tmp_path):
    c = world(tmp_path, [hdev("plug", "Laundry", model="Smart plug", macs=[REAL])])
    put(c, "d-real", "192.168.50.53", REAL, "unknown", "Unknown device")
    put(c, "d-tr", "192.168.50.53", TRANSLATED, "iot", "SH-Laundry", {"hostnames": ["plug-083af2112233.local"]})
    views = {v["id"]: v for v in c.devices()}
    merged = [v for v in views.values() if v["merged_into"]]
    assert len(merged) == 1 and len([v for v in views.values() if not v["merged_into"]]) == 1
    assert views["d-real"]["kind"] in ("iot", "unknown")                              # the secondary is not a row of its own anymore
    prim = next(v for v in views.values() if not v["merged_into"])
    assert [a["mac"] for a in prim["aliases"]] and prim["suggestions"] == []


def test_the_device_list_shows_a_second_interface_inside_its_device():
    js = (ROOT / "lanloom" / "web" / "js" / "views.js").read_text()
    row = js[js.index("const rows = data.devices.filter("):][:200]
    assert "!d.merged_into" in row
    assert "(d.aliases || [])" in js[js.index("const rows = data.devices.filter("):][:700]
    assert "kind === 'hub'" in js


def test_reason_texts_exist_in_every_language():
    for lang in ("de", "en", "es", "fr", "it", "nl"):
        words = json.loads((ROOT / "lanloom" / "i18n" / ("%s.json" % lang)).read_text())
        for how in ("mac", "tracker", "ip"):
            assert words.get("reason.hub." + how), (lang, how)
