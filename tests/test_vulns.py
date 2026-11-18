"""Vulnerability hints (lanloom/vulns.py): version ranges, strict matching (better no hint than a wrong one), the
bundled feed and the import, the run without any network, the nightly section, the event, roles and the audit log,
and the feed build script. Every maker, model and id in this file is invented."""
import datetime
import gzip
import importlib.util
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import core as core_mod
from lanloom import vulns as vm
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.store import Store

ROOT = Path(__file__).resolve().parent.parent
DAY = 86400.0
NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc).timestamp()


# ------------------------------------------------------------------ versions
@pytest.mark.parametrize("a,b,want", [
    ("1.2.3", "1.2.3", 0), ("1.2.3", "1.2.4", -1), ("1.10", "1.9", 1), ("1.2", "1.2.0", 0), ("1.2", "1.2.1", -1),
    ("V5.21(AAZF.13)C0", "5.21(aazf.10)c0", 1), ("5.21(AAZF.7)C0", "5.21\\(aazf.10\\)c0", -1),
    ("5.21(AASZ.3)C0", "5.21(aazf.10)c0", None),            # another hardware branch: never compared
    ("1.0", "1.0rc1", None), ("1.06B01", "1.06b01", 0), ("1.05B03", "1.06b01", -1),
    ("R7-V1.0.11.116_10.2.100", "1.0.9.34", 1), ("Example OS", "1.0", None), ("", "1.0", None), (None, "1.0", None),
    ("2023", "2024", -1), ("build 12", "1.0", None),
])
def test_compare(a, b, want):
    assert vm.compare(a, b) == want


def test_ranges():
    assert vm.in_range("1.2.3", {"lt": "1.3"}) is True
    assert vm.in_range("1.3", {"lt": "1.3"}) is False
    assert vm.in_range("1.3", {"le": "1.3"}) is True
    assert vm.in_range("1.1", {"ge": "1.2", "lt": "1.3"}) is False
    assert vm.in_range("1.2", {"gt": "1.2", "lt": "1.3"}) is False
    assert vm.in_range("1.2.5", {"ge": "1.2", "lt": "1.3"}) is True
    assert vm.in_range("1.05", {"eq": "1.05"}) is True and vm.in_range("1.06", {"eq": "1.05"}) is False
    assert vm.in_range("5.21(aasz.3)c0", {"lt": "5.21(aazf.10)c0"}) is None


def test_maker_and_model_need_exact_words():
    assert vm.vendor_matches("exnet", "ExNet Communications Corp.")
    assert vm.vendor_matches("exlink", "EX-LINK Technologies Co., Ltd.")
    assert not vm.vendor_matches("ex", "Exa Systems")                    # never a part of a word
    assert not vm.vendor_matches("exnet", "Corp ExNet")                  # the maker's name comes first
    assert vm.model_keys("NAS-32", "ExNet") == {"nas32"}
    assert vm.model_keys("ExNet NAS-32", "ExNet Communications") == {"exnetnas32", "nas32"}
    assert vm.model_keys("Router", "ExNet") == set() and vm.model_keys("AB", "x") == set()   # too general
    assert vm.model_keys("DS-2+", "x") == {"ds2plus"} and "ds2" not in vm.model_keys("DS-2+", "x")


# ------------------------------------------------------------------ matching
def feed(*entries):
    base = {"sev": "critical", "score": 9.8, "kev": "", "ransom": False, "title": "t", "action": "update"}
    return vm.Feed.from_raw([dict(base, **e) for e in entries], as_of="2026-11-01")


F = feed({"id": "CVE-2026-0001", "kev": "2026-02-01", "match": [{"v": "exnet", "p": "nas32", "r": [{"lt": "5.21(aazf.10)c0"}]}]},
         {"id": "CVE-2026-0002", "sev": "high", "match": [{"v": "exnet", "p": "nas32", "r": [{"ge": "4.0", "lt": "4.5"}]}]},
         {"id": "CVE-2026-0003", "match": [{"v": "exnet", "p": "gw100", "r": []}]},
         {"id": "CVE-2026-0004", "match": [{"v": "otherco", "p": "nas32", "r": [{"lt": "99"}]}]})


def cand(vendor="ExNet Corp", model="NAS32", fw="V5.21(AAZF.7)C0", fw_src="snmp", vendor_src="upnp", model_src="upnp"):
    return {"vendor": vendor, "vendor_src": vendor_src, "model": model, "model_src": model_src, "firmware": fw, "fw_src": fw_src}


def ids(res):
    return sorted((f["entry"]["id"], f["certainty"]) for f in res["findings"])


def test_certain_hit_with_firmware_from_a_management_interface():
    res = vm.match([cand()], F)
    assert ids(res) == [("CVE-2026-0001", "certain")] and res["status"] == "found"
    f = res["findings"][0]
    assert f["fixed"] == "5.21(aazf.10)c0" and "firmware_in_range" in f["reasons"] and "maker" in f["reasons"]


def test_firmware_read_from_an_announcement_is_only_possible():
    res = vm.match([cand(fw_src="mdns")], F)
    assert ids(res) == [("CVE-2026-0001", "possible")] and "firmware_announced" in res["findings"][0]["reasons"]


def test_maker_only_from_the_address_register_is_only_possible():
    res = vm.match([cand(vendor_src="oui", fw_src="own")], F)
    assert ids(res) == [("CVE-2026-0001", "possible")] and "maker_registry" in res["findings"][0]["reasons"]


def test_every_version_affected_is_possible_even_without_firmware():
    res = vm.match([cand(model="GW-100", fw=None, fw_src=None)], F)
    assert ids(res) == [("CVE-2026-0003", "possible")] and "all_versions" in res["findings"][0]["reasons"]


def test_misses_and_reasons_not_checked():
    assert vm.match([cand(fw="5.21(aazf.12)c0")], F)["status"] == "not_affected"                 # fixed firmware
    assert vm.match([cand(fw="5.21(aasz.3)c0")], F) == {"findings": [], "status": "unchecked", "reason": "firmware_unreadable"}
    assert vm.match([cand(fw=None, fw_src=None)], F)["reason"] == "firmware_unknown"
    assert vm.match([cand(vendor=None)], F)["reason"] == "vendor_unknown"
    assert vm.match([cand(model=None)], F)["reason"] == "model_unknown"
    assert vm.match([cand(model="NAS-99")], F)["status"] == "none"                                  # nothing listed for the model


def test_false_positive_guards():
    # a model that only starts like a listed one
    assert vm.match([cand(model="NAS3")], F)["findings"] == [] and vm.match([cand(model="NAS320")], F)["findings"] == []
    # the same model name from another maker
    assert ids(vm.match([cand(vendor="Otherco")], F)) == [("CVE-2026-0004", "certain")]
    assert vm.match([cand(vendor="Some Other Maker")], F)["findings"] == []
    # a model known only from the address register never counts
    assert vm.match([cand(model_src="oui")], F)["reason"] == "model_unknown"
    # an older major version lies below "before 5.21(aazf.10)c0" too; each range decides on its own
    assert ids(vm.match([cand(fw="4.2", fw_src="own")], F)) == [("CVE-2026-0001", "certain"), ("CVE-2026-0002", "certain")]
    assert ids(vm.match([cand(fw="4.6", fw_src="own")], F)) == [("CVE-2026-0001", "certain")]


def test_the_strongest_candidate_wins():
    res = vm.match([cand(fw_src="mdns"), cand(fw_src="own")], F)
    assert ids(res) == [("CVE-2026-0001", "certain")]


def test_sort_and_action():
    a = {"severity": "high", "exploited": True, "certainty": "certain", "score": 8, "id": "A"}
    b = {"severity": "critical", "exploited": False, "certainty": "possible", "score": 9, "id": "B"}
    c = {"severity": "critical", "exploited": True, "certainty": "possible", "score": 9, "id": "C"}
    assert [x["id"] for x in sorted([a, b, c], key=vm.sort_key)] == ["C", "B", "A"]
    assert vm.action_key({"fixed": "1.2"}) == "vulns.do.update_to"
    assert vm.action_key({"fixed": None, "reasons": ["all_versions"]}) == "vulns.do.replace"
    assert vm.action_key({"action": "replace", "fixed": "1"}) == "vulns.do.replace"
    assert vm.action_key({"fixed": None, "reasons": []}) == "vulns.do.update"


# ------------------------------------------------------------------ the feed file
def test_the_bundled_feed_is_valid_and_offline_readable():
    data = vm.DATA_FILE.read_bytes()
    meta, entries = vm.parse_feed(data)
    assert meta["skipped"] == 0 and meta["entries"] == len(entries) >= 300
    assert sum(1 for e in entries if e["kev"]) >= 50
    assert vm.eol_mod.parse_date(meta["as_of"]) and meta["sources"]
    lines = gzip.decompress(data).splitlines()
    assert json.loads(lines[0])["format"] == 1 and len(lines) - 1 == len(entries)   # a header, then one entry per line; nothing dropped
    for e in entries:
        for m in e["match"]:
            assert vm.TOKEN_RX.fullmatch(m["v"]) and any(ch.isdigit() for ch in m["p"])
            for r in m["r"]:
                assert all(vm.version_core(x) for x in r.values())
    assert len(data) < 2_000_000                                      # budget: a small file in the image


def test_the_feed_is_small_in_memory_and_loaded_only_when_needed(tmp_path):
    import tracemalloc
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    f = vm.Feed.load()
    kept = (tracemalloc.get_traced_memory()[0] - base) / 1e6
    peak = (tracemalloc.get_traced_memory()[1] - base) / 1e6
    tracemalloc.stop()
    assert len(f) >= 300 and kept < 8 and peak < 30, (kept, peak)          # budget (B4): a few megabytes, read line by line
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    c = core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))
    assert c.vulns._feed is None                                               # the start does not pay for it
    assert len(c.vulns.feed) == len(f) and c.vulns._feed is not None


def test_bad_entries_are_skipped_and_bad_files_refused():
    good = {"id": "CVE-2026-1", "match": [{"v": "exnet", "p": "nas32", "r": []}]}
    bad = [{"id": "x", "match": good["match"]}, {"id": "CVE-2026-2", "match": []}, {"id": "CVE-2026-3", "match": [{"v": "Ex Net", "p": "nas32"}]},
           {"id": "CVE-2026-4", "match": [{"v": "exnet", "p": "n3", "r": [{"lt": "abc"}]}]},
           {"id": "CVE-2026-5", "match": [{"v": "exnet", "p": "nas32", "r": [{"eq": "1", "lt": "2"}]}]},
           {"id": "CVE-2026-6", "match": [{"v": "exnet", "p": "nas32", "r": [{"from": "1"}]}]}, "nonsense"]
    doc = {"format": 1, "as_of": "2026-11-01", "entries": [good] + bad}
    meta, entries = vm.parse_feed(json.dumps(doc).encode())
    assert [e["id"] for e in entries] == ["CVE-2026-1"] and meta["skipped"] == len(bad)
    assert vm.parse_feed(gzip.compress(json.dumps(doc).encode()))[1][0]["id"] == "CVE-2026-1"
    for broken in (b"", b"{", b"[]", json.dumps({"format": 2, "as_of": "2026-11-01", "entries": []}).encode(),
                   json.dumps({"format": 1, "entries": []}).encode(), b"\x1f\x8bnot gzip"):
        with pytest.raises(ValueError):
            vm.parse_feed(broken)


def test_import_adds_and_replaces_and_can_be_removed(tmp_path):
    bundled = tmp_path / "bundled.json.gz"
    bundled.write_bytes(gzip.compress(json.dumps({"format": 1, "as_of": "2026-10-01", "entries": [
        {"id": "CVE-2026-1", "sev": "high", "match": [{"v": "exnet", "p": "nas32", "r": []}]}]}).encode()))
    extra = tmp_path / "vulns" / "feed.json.gz"
    f = vm.Feed.load(extra, bundled)
    assert len(f) == 1 and f.imported is None and f.as_of == "2026-10-01"
    extra.parent.mkdir()
    extra.write_bytes(gzip.compress(json.dumps({"format": 1, "as_of": "2026-11-15", "entries": [
        {"id": "CVE-2026-1", "sev": "critical", "match": [{"v": "exnet", "p": "nas32", "r": []}]},
        {"id": "CVE-2026-2", "match": [{"v": "exnet", "p": "gw100", "r": []}]}]}).encode()))
    f = vm.Feed.load(extra, bundled)
    assert len(f) == 2 and f.entries["CVE-2026-1"]["sev"] == "critical" and f.as_of == "2026-11-15" and f.imported["entries"] == 2
    extra.write_bytes(b"broken")
    assert len(vm.Feed.load(extra, bundled)) == 1                    # a broken import never breaks the start


# ------------------------------------------------------------------ the application part
@pytest.fixture()
def core(tmp_path):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    c = core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))
    c.inventory.clock = lambda: NOW
    c.vulns.clock = lambda: NOW
    c.vulns.feed = F
    store.execute("INSERT INTO scans(started, finished, mode, cidrs, status, hosts) VALUES(?,?,?,?,?,?)",
                  (NOW - 4000, NOW - 3600, "gentle", "[]", "done", 0))
    return c


def add(c, n, title, data, kind="nas"):
    did = "d-test-%d" % n
    c.store.upsert_device({"id": did, "ip": "192.168.50.%d" % n, "mac": None, "data": data, "kind": kind, "confidence": 0.9, "title": title}, None)
    c.store.execute("UPDATE devices SET first_seen=?, last_seen=? WHERE id=?", (NOW - 40 * DAY, NOW, did))
    c._refresh_index()
    return did


SSDP = lambda model, maker="ExNet Corp": [{"st": "upnp:rootdevice", "friendly_name": "x", "model": model, "manufacturer": maker,
                                          "device_type": None, "server": None}]


def test_devices_get_hints_from_what_lanloom_knows(core):
    a = add(core, 1, "Storage", {"ssdp": SSDP("NAS32"), "snmp": {"descr": "NAS32, firmware 4.1.2"}})
    b = add(core, 2, "Gateway", {"ssdp": SSDP("GW-100")}, kind="router")
    c_ = add(core, 3, "Laptop", {}, kind="computer")
    d = add(core, 4, "Old storage", {"ssdp": SSDP("NAS32")})
    out = core.vulns.list()
    rows = {(r["device"]["id"], r["id"]): r for r in out["items"]}
    assert set(rows) == {(a, "CVE-2026-0001"), (a, "CVE-2026-0002"), (b, "CVE-2026-0003")}
    assert rows[(a, "CVE-2026-0002")]["firmware"] == "4.1.2" and rows[(a, "CVE-2026-0002")]["firmware_source"] == "snmp"
    assert rows[(a, "CVE-2026-0001")]["certainty"] == "certain" and rows[(a, "CVE-2026-0001")]["exploited"] is True
    assert rows[(b, "CVE-2026-0003")]["certainty"] == "possible"
    assert out["items"][0]["id"] == "CVE-2026-0001"                     # exploited, critical, certain first
    unchecked = {u["device"]["id"]: u["reason"] for u in out["unchecked"]}
    assert unchecked == {c_: "vendor_unknown", d: "firmware_unknown"}
    s = out["summary"]
    assert (s["open"], s["exploited"], s["certain"], s["possible"], s["devices"]) == (3, 1, 2, 1, 2)
    # firmware typed into the inventory decides: the old storage is now known and outside the range
    core.inventory.set_fields(d, {"firmware": "5.21(AAZF.14)C0"})
    core.vulns.invalidate()
    assert core.vulns.device(d)["status"] == "not_affected"
    core.inventory.set_fields(d, {"firmware": "5.21(AAZF.2)C0"})
    core.vulns.invalidate()
    assert [(f["id"], f["certainty"], f["firmware_source"]) for f in core.vulns.device(d)["findings"]] == [("CVE-2026-0001", "certain", "own")]


def test_hub_and_family_facts_count(core):
    a = add(core, 1, "Plug", {}, kind="iot")
    core.hub = {1: {"snapshot": {"devices": [{"id": "hd1", "manufacturer": "ExNet", "model": "NAS32", "sw": "4.1"}]}}}
    core.hub_merge = lambda devices=None: {"by_net": {a: [{"integ": 1, "device_id": "hd1"}]}}
    core.vulns.invalidate()
    assert [(f["id"], f["certainty"], f["firmware_source"]) for f in core.vulns.device(a)["findings"]] == [
        ("CVE-2026-0001", "certain", "hub"), ("CVE-2026-0002", "certain", "hub")]


def test_dismiss_restore_and_retired_devices(core):
    a = add(core, 1, "Gateway", {"ssdp": SSDP("GW-100")}, kind="router")
    core.vulns.dismiss(a, "CVE-2026-0003", "isolated in the lab network", by="admin")
    out = core.vulns.list()
    assert out["items"] == [] and out["summary"]["dismissed"] == 1
    assert core.vulns.list({"show": "dismissed"})["items"][0]["dismissed"]["note"] == "isolated in the lab network"
    core.vulns.restore(a, "CVE-2026-0003")
    assert len(core.vulns.list()["items"]) == 1
    with pytest.raises(ValueError):
        core.vulns.dismiss("nope", "CVE-2026-0003")
    with pytest.raises(ValueError):
        core.vulns.restore(a, "CVE-2026-0003")
    core.inventory.lifecycle(a, "decommission", at="2026-11-17")
    core.vulns.invalidate()
    assert core.vulns.list()["items"] == []                                     # a retired device is no news


def test_new_hints_are_announced_once(core):
    sent = []
    core.notify = SimpleNamespace(emit=lambda *a, **kw: sent.append((a, kw)))
    a = add(core, 1, "Gateway", {"ssdp": SSDP("GW-100")}, kind="router")
    assert [(d["id"], f["id"]) for d, f in core.vulns.watch(NOW)] == [(a, "CVE-2026-0003")]
    assert sent[0][0][:2] == ("vulnerability", "vuln:%s:CVE-2026-0003" % a) and sent[0][1]["once"] is True
    assert sent[0][1]["severity"] == "info"                                     # possible and not exploited: calm
    assert core.vulns.watch(NOW + 3600) == [] and len(sent) == 1
    assert core.vulns.first_seen()["%s|CVE-2026-0003" % a] == NOW


def test_nightly_section(core):
    from lanloom import vulns_report as rep
    ctx = lambda since, until: SimpleNamespace(core=core, since=since, until=until)
    assert rep._available(ctx(NOW - DAY, NOW)) is False
    a = add(core, 1, "Storage", {"ssdp": SSDP("NAS32")})
    core.inventory.set_fields(a, {"firmware": "V5.21(AAZF.7)C0"})
    core.vulns.invalidate()
    core.vulns.watch(NOW - 3600)
    items = rep._new_items(ctx(NOW - 7200, NOW))
    assert [(i["key"], i["level"], i["point"], i["vars"]["fixed"]) for i in items] == [("vulns.report.x.update_to", "warn", True, "5.21(aazf.10)c0")]
    assert rep._new_items(ctx(NOW, NOW + DAY)) == []                            # once, the night it appeared
    sec = rep._build(ctx(NOW - 7200, NOW))
    assert sec["summary"]["vars"] == {"open": 1, "devices": 1, "exploited": 1} and sec["items"][0]["vars"]["name"] == "Storage"
    assert a


def test_feed_grows_old_note(core):
    from lanloom import vulns_report as rep
    t = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc).timestamp() + rep.FEED_OLD_DAYS * DAY
    item = rep._feed_item(SimpleNamespace(core=core, since=t - 3600, until=t + 3600))
    assert item and item["key"] == "vulns.report.feed_old"
    assert rep._feed_item(SimpleNamespace(core=core, since=t + 3600, until=t + 7200)) is None


def test_everything_runs_without_any_network(core, monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    core.vulns.feed = vm.Feed.load(core.vulns.extra)                            # the real bundled feed
    add(core, 1, "Storage", {"ssdp": SSDP("NAS32"), "snmp": {"descr": "NAS32, firmware 5.21"}})
    out = core.vulns.list()
    assert out["feed"]["entries"] >= 300 and isinstance(out["items"], list)
    core.vulns.watch(NOW)


# ------------------------------------------------------------------ the build script
def _script():
    spec = importlib.util.spec_from_file_location("build_vulnfeed", ROOT / "scripts" / "build_vulnfeed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cpe(part, vendor, product, version="*"):
    return "cpe:2.3:%s:%s:%s:%s:*:*:*:*:*:*:*" % (part, vendor, product, version)


def test_build_script(tmp_path, monkeypatch):
    monkeypatch.setenv("VULNFEED_SMALL", "1")
    kev = {"catalogVersion": "2026.11.01", "vulnerabilities": [
        {"cveID": "CVE-2026-1001", "vulnerabilityName": "ExNet NAS command injection", "dateAdded": "2026-03-01",
         "knownRansomwareCampaignUse": "Known", "requiredAction": "Apply updates per vendor instructions."},
        {"cveID": "CVE-2026-1003", "vulnerabilityName": "ExNet GW end of life", "dateAdded": "2026-04-01",
         "knownRansomwareCampaignUse": "Unknown", "requiredAction": "The impacted product is end-of-life. Users should discontinue the product."}]}
    metric = lambda sev, score: {"cvssMetricV31": [{"type": "Primary", "cvssData": {"baseSeverity": sev, "baseScore": score}}]}
    nvd = {"format": "NVD_CVE", "vulnerabilities": [
        {"cve": {"id": "CVE-2026-1001", "metrics": metric("HIGH", 8.8), "descriptions": [{"lang": "en", "value": "x"}], "configurations": [
            {"operator": "AND", "nodes": [
                {"operator": "OR", "cpeMatch": [{"vulnerable": True, "criteria": _cpe("o", "exnet", "nas32_firmware"), "versionEndExcluding": "5.21\\(aazf.10\\)c0"}]},
                {"operator": "OR", "cpeMatch": [{"vulnerable": False, "criteria": _cpe("h", "exnet", "nas32", "-")}]}]}]}},
        {"cve": {"id": "CVE-2026-1002", "metrics": metric("CRITICAL", 9.8),
                 "descriptions": [{"lang": "en", "value": "A stack overflow in the ExNet RX-7 web server allows code execution. More text."}],
                 "configurations": [{"nodes": [{"operator": "OR", "cpeMatch": [
                     {"vulnerable": True, "criteria": _cpe("o", "exnet", "rx-7_firmware", "1.0.2")},
                     {"vulnerable": True, "criteria": _cpe("a", "exnet", "cloud_app"), "versionEndExcluding": "3"}]}]}]}},
        {"cve": {"id": "CVE-2026-1003", "metrics": metric("MEDIUM", 6.0), "configurations": [{"nodes": [{"operator": "OR", "cpeMatch": [
            {"vulnerable": True, "criteria": _cpe("h", "exnet", "gw100", "-")}]}]}]}},
        {"cve": {"id": "CVE-2026-1004", "metrics": metric("HIGH", 7.5), "configurations": [{"nodes": [{"operator": "OR", "cpeMatch": [
            {"vulnerable": True, "criteria": _cpe("o", "exnet", "nas40_firmware"), "versionEndExcluding": "2"}]}]}]}},   # neither listed nor critical
        {"cve": {"id": "CVE-2026-1005", "metrics": metric("CRITICAL", 10.0), "configurations": [{"nodes": [{"operator": "OR", "cpeMatch": [
            {"vulnerable": True, "criteria": _cpe("o", "exnet", "desktop_os"), "versionEndExcluding": "2"}]}]}]}},         # no device firmware
    ]}
    (tmp_path / "kev.json").write_text(json.dumps(kev))
    (tmp_path / "nvd.json").write_text(json.dumps(nvd))
    out = tmp_path / "feed.json.gz"
    assert _script().main(["--kev", str(tmp_path / "kev.json"), "--nvd", str(tmp_path / "nvd.json"), "--out", str(out),
                           "--date", "2026-11-18", "--min-entries", "1"]) == 0
    meta, entries = vm.parse_feed(out.read_bytes())
    by = {e["id"]: e for e in entries}
    assert set(by) == {"CVE-2026-1001", "CVE-2026-1002", "CVE-2026-1003"} and meta["as_of"] == "2026-11-18"
    assert by["CVE-2026-1001"]["match"] == [{"v": "exnet", "p": "nas32", "r": [{"lt": "5.21(aazf.10)c0"}]}]
    assert by["CVE-2026-1001"]["kev"] == "2026-03-01" and by["CVE-2026-1001"]["ransom"] is True and by["CVE-2026-1001"]["sev"] == "high"
    assert by["CVE-2026-1002"]["match"] == [{"v": "exnet", "p": "rx7", "r": [{"eq": "1.0.2"}]}]
    assert by["CVE-2026-1002"]["title"] == "A stack overflow in the ExNet RX-7 web server allows code execution"
    assert by["CVE-2026-1003"]["match"] == [{"v": "exnet", "p": "gw100", "r": []}] and by["CVE-2026-1003"]["action"] == "replace"
    # the same bytes for the same input (no time stamp in the file)
    first = out.read_bytes()
    _script().main(["--kev", str(tmp_path / "kev.json"), "--nvd", str(tmp_path / "nvd.json"), "--out", str(out), "--date", "2026-11-18", "--min-entries", "1"])
    assert out.read_bytes() == first
    with pytest.raises(SystemExit):
        _script().main(["--kev", str(tmp_path / "kev.json"), "--nvd", str(tmp_path / "nvd.json"), "--out", str(out), "--min-entries", "50"])


# ------------------------------------------------------------------ HTTP: demo, roles, import, audit
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


def test_demo_shows_every_case(app_client):
    c = app_client
    data = c.get("/api/vulns").json()
    rows = {r["id"]: r for r in data["items"]}
    # the TV's firmware comes from the sample hub, which only runs in a started demo (live sources); without it the TV is "not checked"
    assert {"DEMO-2026-0101", "DEMO-2025-0201"} <= set(rows) <= {"DEMO-2026-0101", "DEMO-2026-0401", "DEMO-2025-0201"}
    if "DEMO-2026-0401" in rows:
        assert rows["DEMO-2026-0401"]["certainty"] == "certain" and rows["DEMO-2026-0401"]["firmware_source"] == "hub"
    gw = rows["DEMO-2026-0101"]
    assert (gw["certainty"], gw["exploited"], gw["fixed"], gw["firmware_source"]) == ("certain", True, "3.2.0", "own")
    assert rows["DEMO-2025-0201"]["certainty"] == "possible" and "all_versions" in rows["DEMO-2025-0201"]["reasons"]
    assert data["items"][0]["id"] == "DEMO-2026-0101"
    assert data["summary"]["dismissed"] == 1 and data["feed"]["demo"] is True
    reasons = {u["reason"] for u in data["unchecked"]}
    assert {"firmware_unknown", "vendor_unknown"} <= reasons
    dismissed = c.get("/api/vulns?show=dismissed").json()["items"]
    assert [r["id"] for r in dismissed] == ["DEMO-2026-0301"] and dismissed[0]["dismissed"]["note"]
    assert c.get("/api/devices/%s/vulns" % gw["device"]["id"]).json()["findings"][0]["id"] == "DEMO-2026-0101"
    assert c.get("/api/devices/nope/vulns").status_code == 404


def test_http_roles_audit_and_import(app_client):
    c = app_client
    st = c.st
    gw = c.get("/api/vulns").json()["items"][0]
    body = {"device": gw["device"]["id"], "id": gw["id"], "note": "secret reason text"}
    assert c.post("/api/vulns/dismiss", json=body, headers=H).status_code == 200
    assert c.post("/api/vulns/restore", json=body, headers=H).status_code == 200
    assert c.post("/api/vulns/restore", json=body, headers=H).json() == {"error": "vulns.err.unknown"}
    feed = gzip.compress(json.dumps({"format": 1, "as_of": "2026-11-17", "entries": [
        {"id": "CVE-2026-9", "sev": "high", "match": [{"v": "exnet", "p": "nas32", "r": []}]}]}).encode())
    r = c.post("/api/vulns/feed", content=feed, headers=dict(H, **{"content-type": "application/octet-stream"}))
    assert r.status_code == 200 and r.json()["entries"] == 1
    assert st.core.vulns.feed.imported["entries"] == 1
    assert c.post("/api/vulns/feed", content=b"nonsense", headers=dict(H, **{"content-type": "application/octet-stream"})).json() == {"error": "vulns.err.feed"}
    assert c.delete("/api/vulns/feed", headers=H).json()["imported"] is None
    acts = [(e["action"], e["outcome"]) for e in st.audit.query({}, 50)]
    for want in ("vulns.dismiss", "vulns.restore", "vulns.feed_import", "vulns.feed_remove"):
        assert (want, "ok") in acts, want
    assert "secret reason text" not in json.dumps(st.audit.query({}, 50))
    st.accounts.create("view", "a-long-test-password", role="viewer")
    st.accounts.create("op", "a-long-test-password", role="operator")

    def as_(user):
        c.cookies.clear()
        assert c.post("/api/login", json={"username": user, "password": "a-long-test-password"}, headers=H).status_code == 200

    def codes():
        return (c.get("/api/vulns").status_code, c.get("/api/vulns/feed").status_code,
                c.post("/api/vulns/dismiss", json=body, headers=H).status_code,
                c.post("/api/vulns/feed", content=feed, headers=dict(H, **{"content-type": "application/octet-stream"})).status_code,
                c.delete("/api/vulns/feed", headers=H).status_code)
    as_("view")
    assert codes() == (200, 200, 403, 403, 403)
    as_("op")
    assert codes() == (200, 200, 200, 403, 403)
