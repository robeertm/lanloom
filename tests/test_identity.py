"""Manufacturer register (offline), origin classes, fingerprints and "one device, several interfaces"."""
import gzip
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import access, classify, core, demo, identity, mapmodel, oui
from lanloom.store import Store

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------- register
@pytest.fixture(scope="module")
def reg():
    r = oui.Oui()
    r.start(background=False)
    assert r.db is not None, r.error
    return r


def test_bundled_register_loads_and_is_complete(reg):
    info = reg.info()
    assert info["loaded"] and info["rows"] >= oui.MIN_ROWS
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", info["date"])
    assert {24, 28, 36} == set(reg.db.tables)
    assert all(len(reg.db.tables[b][0]) > 1000 for b in (24, 28, 36))


def test_register_file_is_small_and_never_fetched_at_run_time():
    assert oui.DATA_FILE.stat().st_size < 1_500_000
    src = (ROOT / "lanloom" / "oui.py").read_text()
    for forbidden in ("urllib", "http.client", "socket", "requests"):
        assert forbidden not in src


def test_random_and_group_addresses_never_get_a_maker(reg):
    assert oui.is_random("da:a1:19:00:00:01") and reg.lookup("da:a1:19:00:00:01") is None
    assert reg.origin_of("da:a1:19:00:00:01") == oui.RANDOM
    assert reg.origin_of("01:00:5e:00:00:fb") == oui.NO_MAC          # multicast group
    assert reg.origin_of(None) == oui.NO_MAC and reg.origin_of("nonsense") == oui.NO_MAC
    assert reg.origin_of("00:00:00:00:00:00") == oui.NO_MAC


def test_longest_prefix_wins(reg):
    # pick a 36-bit block: its address must resolve to the small block's holder, not the enclosing 24-bit holder
    prefixes, idx = reg.db.tables[36]
    i = next(i for i in range(len(prefixes)) if reg.db.names[idx[i]])
    v = prefixes[i] << 12
    mac = ":".join("%02x" % ((v >> s) & 0xFF) for s in range(40, -8, -8))
    assert reg.lookup(mac) == reg.db.names[idx[i]]
    assert reg.origin_of(mac) == oui.REGISTERED


def test_normalises_address_spellings(reg):
    prefixes, idx = reg.db.tables[24]
    i = next(i for i in range(len(prefixes)) if reg.db.names[idx[i]])
    hexs = "%06x" % prefixes[i]
    want = reg.db.names[idx[i]]
    assert reg.lookup("%s:%s:%s:12:34:56" % (hexs[0:2], hexs[2:4], hexs[4:6])) == want
    assert reg.lookup(("%s-%s-%s-12-34-56" % (hexs[0:2], hexs[2:4], hexs[4:6])).upper()) == want
    assert reg.lookup(hexs + "123456") == want


def _tiny(date="2026-01-01", names=("", "Alpha Devices", "Beta Labs")):
    return oui.pack({"date": date}, list(names), {24: [(0x001122, 1)], 28: [(0x0011223, 2)], 36: [(0x001122334, 0)]})


def test_tampered_file_is_refused(tmp_path):
    raw = bytearray(_tiny())
    raw[-3] ^= 0xFF
    with pytest.raises(oui.OuiError):
        oui.OuiDB(bytes(raw))
    p = tmp_path / "bad.bin.gz"
    p.write_bytes(b"not gzip")
    r = oui.Oui(path=p)
    r.start(background=False)
    assert r.db is None and r.lookup("00:11:22:00:00:01") is None and not r.info()["loaded"]


def test_tiny_table_lookup_order():
    db = oui.OuiDB(_tiny())
    assert db.lookup("00:11:22:40:00:00") == "Alpha Devices"
    assert db.lookup("00:11:22:30:00:00") == "Beta Labs"          # 28-bit block inside the 24-bit block
    assert db.lookup("00:11:22:33:40:00") is None                  # registry's own "private" entry: no name


def test_newer_drop_in_wins_only_when_valid_and_newer(tmp_path):
    bundled = tmp_path / "b.bin.gz"
    bundled.write_bytes(gzip.compress(_tiny("2026-01-01")))
    newer = tmp_path / "n.bin.gz"
    newer.write_bytes(gzip.compress(_tiny("2026-06-01", ("", "Gamma Works", "Beta Labs"))))
    r = oui.Oui(path=bundled, extra=newer)
    r.db = None
    r._load = r._load  # noqa: B018
    # tiny tables are below MIN_ROWS: allow them for this test
    orig = oui.OuiDB.from_file
    oui.OuiDB.from_file = classmethod(lambda cls, p, min_rows=0: orig.__func__(cls, p, 0))
    try:
        r.start(background=False)
        assert r.origin == "data-dir" and r.lookup("00:11:22:40:00:00") == "Gamma Works"
        older = oui.Oui(path=newer, extra=bundled)
        older.start(background=False)
        assert older.origin == "bundled" and older.lookup("00:11:22:40:00:00") == "Gamma Works"
    finally:
        oui.OuiDB.from_file = orig


def test_build_script_round_trip(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_oui", ROOT / "scripts" / "build_oui.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "MIN_ROWS", {"MA-L": 2, "MA-M": 1, "MA-S": 1})
    head = "Registry,Assignment,Organization Name,Organization Address\n"
    (tmp_path / "l.csv").write_text(head + 'MA-L,001122,"Alpha Devices Co., Ltd.",x\nMA-L,A8BBCC,IEEE Registration Authority,y\n')
    (tmp_path / "m.csv").write_text(head + "MA-M,A8BBCC1,Beta Labs GmbH,x\n")
    (tmp_path / "s.csv").write_text(head + "MA-S,A8BBCC200,Private,x\n")
    raw = mod.build([tmp_path / "l.csv", tmp_path / "m.csv", tmp_path / "s.csv"], "2026-02-02")
    db = oui.OuiDB(raw)
    assert db.lookup("00:11:22:00:00:01") == "Alpha Devices"
    assert db.lookup("a8:bb:cc:10:00:00") == "Beta Labs"
    assert db.lookup("a8:bb:cc:20:00:00") is None and db.lookup("a8:bb:cc:90:00:00") is None
    (tmp_path / "bad.csv").write_text(head + "MA-L,XYZ,Nope,x\n")
    with pytest.raises(SystemExit):
        mod.read(tmp_path / "bad.csv")


def test_clean_name():
    assert oui.clean_name("Alpha  Devices Co., Ltd.") == "Alpha Devices"
    assert oui.clean_name("Beta Labs GmbH & Co. KG") == "Beta Labs"
    assert oui.clean_name("Gamma, Inc.") == "Gamma"
    assert oui.clean_name("AB") == "AB"


# ---------------------------------------------------------------------- fingerprint
def test_fingerprint_prefers_what_the_device_says():
    r = oui.Oui()
    r.override = {"names": {"00:11:22:00:00:01": "Register Name"}, "random": set()}
    fp = identity.fingerprint({"mac": "00:11:22:00:00:01",
                               "ssdp": [{"manufacturer": "Self Maker", "model": "M-1", "server": "OS/1 UPnP/1.0 dev/3.2.1", "udn": "abc-123-456-789"}],
                               "mdns": [{"type": "_ipp._tcp", "txt": {"ty": "Printer P-9", "fw": "1.0.7"}}]}, r)
    assert fp["vendor"] == "Self Maker" and fp["vendor_source"] == "upnp"
    assert fp["model"] in ("M-1", "Printer P-9") and fp["firmware"] in ("3.2.1", "1.0.7")
    assert fp["origin"] == oui.REGISTERED and fp["registered_name"] == "Register Name"
    assert fp["features"]["upnp_udn"] == "abc-123-456-789" and fp["features"]["oui_prefix"] == "00:11:22"
    assert 0 < fp["confidence"] <= 1


def test_fingerprint_snmp_and_register_only():
    r = oui.Oui()
    r.override = {"names": {"00:11:22:00:00:02": "Register Name"}, "random": set()}
    fp = identity.fingerprint({"mac": "00:11:22:00:00:02", "snmp": {"descr": "Managed switch SW-8, firmware 2.4.1",
                                                                     "object_id": "1.3.6.1.4.1.32473.1", "enterprise": 32473, "name": "sw"}}, r)
    assert fp["vendor"] == "Register Name" and fp["vendor_source"] == "oui"
    assert fp["model"] == "Managed switch SW-8" and fp["firmware"] == "2.4.1"
    assert fp["features"]["enterprise"] == 32473 and fp["features"]["sys_object_id"].startswith("1.3.6.1.4.1.")
    random_fp = identity.fingerprint({"mac": "da:00:00:00:00:01"}, oui.Oui())
    assert random_fp["origin"] == oui.RANDOM and random_fp["vendor"] is None and random_fp["features"]["random_mac"]


def test_classifier_uses_new_evidence():
    v = classify.classify({"ip": "192.168.1.5", "wsd": [{"types": ["dn:NetworkVideoTransmitter"]}]})
    assert v["kind"] == "camera" and "wsd:networkvideotransmitter" in v["reasons"]
    v = classify.classify({"ip": "192.168.1.6", "wsd": [{"types": ["wprt:PrintDeviceType"]}], "ports": [{"port": 631}]})
    assert v["kind"] == "printer"
    v = classify.classify({"ip": "192.168.1.7", "netbios": {"computer": "DESK"}, "ports": [{"port": 445}, {"port": 3389}]})
    assert v["kind"] == "computer"
    alone = classify.classify({"ip": "192.168.1.8"}, vendor="Example Camera Systems")
    assert alone["kind"] == "unknown"                                    # the maker's name alone never decides
    v = classify.classify({"ip": "192.168.1.8", "ports": [{"port": 554}]}, vendor="Example Camera Systems")
    assert v["kind"] == "camera" and "vendor:camera" in v["reasons"]


# ---------------------------------------------------------------------- grouping
def dev(i, mac=None, scan=1, first=1000.0, **data):
    return {"id": i, "ip": data.pop("ip", "192.168.1.%d" % (len(i) + 1)), "mac": mac, "first_seen": first, "last_scan": scan,
            "data": dict(data, mac=mac)}


def test_same_udn_merges_and_primary_is_the_richer_device():
    a = dev("d-a", "02:00:00:00:00:01", ssdp=[{"udn": "u-1"}], ports=[{"port": 22}, {"port": 445}])
    b = dev("d-b", "02:00:00:00:00:99", ssdp=[{"udn": "u-1"}])
    g = identity.groups([a, b])
    assert g["members"] == {"d-b": {"primary": "d-a", "confidence": 0.9, "reasons": ["same_udn"], "manual": False}}
    assert g["aliases"] == {"d-a": ["d-b"]}


def test_name_evidence_needs_both_devices_current():
    a = dev("d-a", "02:00:00:00:00:01", hostnames=["nas.local"], scan=5)
    b = dev("d-b", "02:00:00:00:00:02", hostnames=["nas.local"], scan=4)       # an older record: replaced device
    assert identity.groups([a, b])["members"] == {}
    b["last_scan"] = 5
    assert [m["reasons"] for m in identity.groups([a, b])["members"].values()] == [["same_mdns"]]
    c = dev("d-c", "02:00:00:00:00:03", hostnames=["device.local"], scan=5)
    d = dev("d-d", "02:00:00:00:00:04", hostnames=["device.local"], scan=5)
    assert identity.groups([c, d])["members"] == {}                              # generic names never merge


def test_neighbouring_addresses_are_a_suggestion_and_with_port_a_merge():
    a = dev("d-a", "4c:4c:00:00:00:07", first=1000)
    b = dev("d-b", "4c:4c:00:00:00:08", first=1100)                              # across the block boundary
    g = identity.groups([a, b])
    assert g["members"] == {} and g["suggestions"][0]["reasons"] == ["adjacent_mac", "first_seen_together"]
    a["link_port"] = b["link_port"] = "d-sw#5"
    g = identity.groups([a, b])
    assert set(g["members"]["d-a" if "d-a" in g["members"] else "d-b"]["reasons"]) >= {"adjacent_mac", "same_port"}
    far = dev("d-c", "4c:4c:00:00:00:20")
    rnd1, rnd2 = dev("d-r1", "02:4c:00:00:00:01"), dev("d-r2", "02:4c:00:00:00:02")
    assert identity.groups([a, far])["suggestions"] == [] and identity.groups([rnd1, rnd2])["suggestions"] == []


def test_user_split_and_join_win():
    a = dev("d-a", "02:00:00:00:00:01", ssdp=[{"udn": "u-1"}])
    b = dev("d-b", "02:00:00:00:00:02", ssdp=[{"udn": "u-1"}])
    c = dev("d-c", "02:00:00:00:00:03", ssdp=[{"udn": "u-1"}])
    assert identity.groups([a, b], splits=[["d-a", "d-b"]])["members"] == {}
    g = identity.groups([a, b, c], splits=[["d-a", "d-b"]])                       # no detour through a third device
    prim = {m["primary"] for m in g["members"].values()} | set(g["aliases"])
    for p, members in g["aliases"].items():
        assert not ({"d-a", "d-b"} <= set(members) | {p})
    x, y = dev("d-x", "02:00:00:00:00:10"), dev("d-y", "02:00:00:00:00:50")
    g = identity.groups([x, y], joins=[["d-y", "d-x"]])
    assert list(g["members"].values())[0]["manual"] is True
    assert prim


# ---------------------------------------------------------------------- core, map, demo, report
@pytest.fixture()
def demo_core():
    st = Store(":memory:")
    c = core.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=True), st, None)
    c.demo = demo.Demo()
    c.demo.seed(c)
    return c


def test_demo_shows_a_merge_a_suggestion_and_every_origin(demo_core):
    views = {d["ip"]: d for d in demo_core.devices()}
    assert views["192.168.50.16"]["merged_into"]["id"] == views["192.168.50.10"]["id"]
    assert [a["ip"] for a in views["192.168.50.10"]["aliases"]] == ["192.168.50.16"]
    assert views["192.168.50.66"]["suggestions"][0]["ip"] == "192.168.50.67"
    assert {d["origin"] for d in views.values()} >= {oui.REGISTERED, oui.RANDOM, oui.UNREGISTERED}
    assert views["192.168.50.40"]["model"] == "Office Laser P-200"
    assert views["192.168.50.10"]["ipv6"] and views["192.168.50.21"]["netbios"]["computer"] == "STUDIO-PC"
    for d in views.values():                      # the demo register is fictional
        assert d["vendor"] is None or d["vendor"].startswith("Example ")
    comps = {c["id"] for c in mapmodel.build(demo_core, "overview")["components"]}
    assert views["192.168.50.16"]["id"] not in comps and views["192.168.50.10"]["id"] in comps


def test_split_shows_the_interface_again_and_reset_merges(demo_core):
    views = {d["ip"]: d for d in demo_core.devices()}
    a, b = views["192.168.50.10"]["id"], views["192.168.50.16"]["id"]
    demo_core.set_identity(a, "split", b)
    assert not demo_core.is_merged(b)
    assert b in {c["id"] for c in mapmodel.build(demo_core, "overview")["components"]}
    demo_core.set_identity(a, "reset", b)
    assert demo_core.is_merged(b)
    s, t = views["192.168.50.66"]["id"], views["192.168.50.67"]["id"]
    demo_core.set_identity(s, "join", t)
    assert demo_core.is_merged(t) or demo_core.is_merged(s)
    for bad in (("split", s), ("nope", t), ("join", "d-missing")):
        with pytest.raises(ValueError):
            demo_core.set_identity(s, *bad)


def test_second_interface_is_not_a_new_device_and_new_devices_name_their_maker(demo_core):
    w = demo_core.reports.watch
    w.known = {d["id"] for d in demo_core.store.devices()} - {"d-" + demo._hmac(16).replace(":", "")}
    w.seeded = True
    n0 = len(demo_core.store.query("SELECT * FROM events WHERE kind='dev_new'"))
    w._devices(time.time())
    assert len(demo_core.store.query("SELECT * FROM events WHERE kind='dev_new'")) == n0     # second NIC of known storage
    w.known.discard("d-" + demo._hmac(40).replace(":", ""))                                                         # the printer, as if new
    w._devices(time.time())
    ev = demo_core.store.query("SELECT * FROM events WHERE kind='dev_new' ORDER BY id DESC LIMIT 1")[0]
    data = json.loads(ev["data"])
    assert data["vendor"] == "Example Imaging" and data["origin"] == oui.REGISTERED


def test_report_texts_for_new_devices_exist_in_every_language():
    from lanloom.nightly import contrib
    for lang in ("en", "de"):
        strings = json.loads((ROOT / "lanloom" / "i18n" / ("%s.json" % lang)).read_text())
        for key in list(contrib.NEW_BY_ORIGIN.values()) + ["report.devices.new_vendor", "report.devices.new"]:
            assert "{name}" in strings[key] and "{at}" in strings[key]


def test_identity_route_needs_operator_and_is_audited():
    rule = access.RULES[("POST", "/api/devices/{device_id}/identity")]
    assert rule.role == "operator" and rule.audit == "device.identity"


def test_snmp_public_is_off_unless_chosen():
    from lanloom import scan
    assert scan.make_plan(["192.168.1.0/24"])["snmp_public"] is False
    assert scan.make_plan(["192.168.1.0/24"], deep=True, consent=True)["snmp_public"] is False
    assert core.DEFAULT_SETTINGS["scan_snmp_public"] is False


# ---------------------------------------------------------------------- repeater rewrites the hardware address
def test_repeater_rewritten_address_with_both_facts_is_one_device_one_fact_a_suggestion():
    a = dev("d-a", "4c:4c:00:11:22:33", scan=1, ip="192.168.1.9")                                   # the old entry
    b = dev("d-b", "52:c7:be:11:22:33", scan=5, ip="192.168.1.9", hostnames=["plug-4c4c00112233.local"])
    g = identity.groups([a, b])                       # same IP and the real address in the name: two facts, merged
    assert g["suggestions"] == [] and set(g["members"]) | {m["primary"] for m in g["members"].values()} == {"d-a", "d-b"}
    only_ip = identity.groups([dict(b, data=dict(b["data"], hostnames=[])), a])
    assert only_ip["members"] == {}
    assert [s["reasons"] for s in only_ip["suggestions"]] == [["translated_mac", "same_ip"]]
    assert identity.PROBABLE <= only_ip["suggestions"][0]["confidence"] < identity.SURE
    only_name = identity.groups([a, dict(b, ip="192.168.1.77")])
    assert only_name["members"] == {}
    assert [s["reasons"] for s in only_name["suggestions"]] == [["translated_mac", "mac_in_name"]]
    # the real address can also be in a DNS-SD instance name
    c = dev("d-c", "52:c7:be:11:22:33", scan=5, ip="192.168.1.78", mdns=[{"type": "_http._tcp", "name": "plug-4c4c00112233"}])
    assert [s["reasons"] for s in identity.groups([a, c])["suggestions"]] == [["translated_mac", "mac_in_name"]]
    # both facts via the DNS-SD name: merged, and the reasons name both
    d = dev("d-d", "52:c7:be:11:22:33", scan=5, ip="192.168.1.9", mdns=[{"type": "_http._tcp", "name": "plug-4c4c00112233"}])
    g = identity.groups([a, d])
    assert g["suggestions"] == [] and list(g["members"].values())[0]["reasons"] == ["translated_mac", "same_ip", "mac_in_name"]


def test_repeater_pattern_needs_a_second_fact_and_a_private_address():
    a = dev("d-a", "4c:4c:00:11:22:33", ip="192.168.1.9")
    no_fact = dev("d-b", "52:c7:be:11:22:33", ip="192.168.1.10")                    # same last bytes, nothing else
    assert identity.groups([a, no_fact])["suggestions"] == []
    both_registered = dev("d-b", "4c:4d:00:11:22:33", ip="192.168.1.9")             # two registered addresses
    assert identity.groups([a, both_registered])["suggestions"] == []
    other_bytes = dev("d-b", "52:c7:be:11:22:34", ip="192.168.1.9")                 # same IP, other last bytes (replaced device)
    assert identity.groups([a, other_bytes])["suggestions"] == []
    assert identity.groups([a, no_fact], splits=[["d-a", "d-b"]])["suggestions"] == []


def test_a_hidden_entry_never_becomes_the_primary_of_a_group():
    a = dev("d-a", "4c:4c:00:11:22:33", scan=1, first=500.0, ip="192.168.1.9", ports=[{"port": 80}, {"port": 443}])
    b = dev("d-b", "52:c7:be:11:22:33", scan=5, first=2000.0, ip="192.168.1.9")
    assert identity.groups([a, b], joins=[["d-a", "d-b"]])["aliases"] == {"d-a": ["d-b"]}
    a["hidden"] = 1
    assert identity.groups([a, b], joins=[["d-a", "d-b"]])["aliases"] == {"d-b": ["d-a"]}


def test_maker_from_the_real_address_inside_the_announced_name():
    r = oui.Oui()
    r.override = {"names": {"4c:4c:00:11:22:33": "Register Name"}, "random": {"52:c7:be:11:22:33", "52:c7:be:11:22:34"}}
    data = {"mac": "52:c7:be:11:22:33", "hostnames": ["plug-4c4c00112233.local"]}
    fp = identity.fingerprint(data, r)
    assert fp["origin"] == oui.RANDOM and fp["vendor"] == "Register Name" and fp["vendor_source"] == "name"
    assert fp["features"]["name_mac"] == "4c:4c:00:11:22:33" and fp["confidence"] <= 0.35
    # only when the last three bytes match the address on the network
    assert identity.fingerprint(dict(data, mac="52:c7:be:11:22:34"), r)["vendor"] is None
    # a private address inside the name is never a maker; neither is a longer hex string
    assert identity.fingerprint(dict(data, hostnames=["plug-024c00112233.local"]), r)["vendor"] is None
    assert identity.fingerprint(dict(data, hostnames=["x-4c4c00112233ab.local"]), r)["vendor"] is None


def test_smart_plug_announcement_makes_a_smart_home_device():
    host = {"ip": "192.168.1.53", "mac": "52:c7:be:11:22:33",
            "mdns": [{"type": "_shelly._tcp", "name": "plug-aabbccddeeff", "txt": {}}]}
    v = classify.classify(host)
    assert v["kind"] == "iot" and "mdns:_shelly._tcp" in v["reasons"]
