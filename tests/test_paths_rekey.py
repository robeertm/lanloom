"""Data paths follow a device whose id changes (lanloom#13): this host first known by its address, then by its hardware
address; an address row merged into the entry with the hardware address; the "self" placeholder; leftovers of older
versions moved once at start, with a copy kept; a former id is never shown raw."""
import re
from pathlib import Path
from types import SimpleNamespace

from lanloom import datapaths
from lanloom.core import Core
from lanloom.store import Store

ROOT = Path(__file__).resolve().parent.parent
SELF_IP = "192.168.50.28"
SELF_MAC = "02:4c:4c:00:00:1c"
SELF_ID = "d-024c4c00001c"
OLD_ID = "d-ip-192-168-50-28"
T0 = 1_790_000_000.0


class FakeGeo:
    db = object()

    def country(self, ip):
        return {"3.3.3.3": "US", "5.5.5.5": "GB"}.get(ip, "FR")

    def info(self):
        return {"loaded": True}


def make_paths(now=T0):
    st = Store(":memory:")
    clock = {"t": now}
    dp = datapaths.DataPaths(st, FakeGeo(), lambda: {"home_country": "DE"}, clock=lambda: clock["t"])
    return dp


def rows(store, table, device):
    return store.query("SELECT * FROM %s WHERE device=?" % table, (device,))


def self_host(with_mac: bool) -> dict:
    return {"ip": SELF_IP, "mac": SELF_MAC if with_mac else None, "hostnames": [], "sources": ["tcp"],
            "ports": [{"port": 22, "proto": "tcp", "service": "ssh", "banner": None}], "mdns": [], "ssdp": [], "ipv6": [],
            "wsd": [], "netbios": None, "snmp": None, "is_gateway": False, "is_self": True, "rtt_ms": 0.1}


def make_core(tmp_path, store=None):
    cfg = SimpleNamespace(data_dir=tmp_path, demo=False, probe=True)
    c = Core(cfg, store or Store(":memory:"), None)
    c.geo.start(background=False)
    return c


def host_flow(c, peer="3.3.3.3", tx=1000, rx=9000):
    c.on_event("host", None, {"type": "bytes", "flows": [{"local_ip": SELF_IP, "peer_ip": peer, "peer_port": 443, "tx": tx, "rx": rx}]})


def booked_ids(c) -> set:
    c.paths.flush()
    out = set()
    for table in ("paths_hour", "paths_day", "paths_seen"):
        out |= {r["device"] for r in c.store.query("SELECT device FROM %s" % table)}
    return out


# ---------------------------------------------------------------------------------------------- the book itself
def test_rename_moves_pending_and_stored_rows_and_adds_sums():
    dp = make_paths()
    dp.book("old", "3.3.3.3", 100, 200)
    dp.book("new", "3.3.3.3", 1, 2)
    dp.flush()
    dp.book("old", "3.3.3.3", 10, 20)            # still pending
    dp.book("old", "5.5.5.5", 7, 0)
    assert dp.rename_device("old", "new") > 0
    dp.flush()
    assert not rows(dp.store, "paths_hour", "old") and not rows(dp.store, "paths_seen", "old")
    us = {r["cc"]: (r["tx"], r["rx"]) for r in rows(dp.store, "paths_hour", "new")}
    assert us == {"US": (111, 222), "GB": (7, 0)}
    assert {r["cc"]: (r["tx"], r["rx"]) for r in rows(dp.store, "paths_day", "new")} == us
    assert {r["cc"] for r in rows(dp.store, "paths_seen", "new")} == {"US", "GB"}
    assert dp.rename_device("old", "new") == 0                       # idempotent


def test_rename_keeps_first_and_last_seen_of_both():
    dp = make_paths()
    dp.store.execute("INSERT INTO paths_seen(device, cc, first, last) VALUES('old', 'US', 10, 50)")
    dp.store.execute("INSERT INTO paths_seen(device, cc, first, last) VALUES('new', 'US', 20, 40)")
    dp.rename_device("old", "new")
    r = rows(dp.store, "paths_seen", "new")[0]
    assert (r["first"], r["last"]) == (10, 50)


def test_rename_turns_conversations_around_and_drops_the_one_with_itself():
    dp = make_paths()
    dp.book_pair("zz-old", "aa", 100)            # stored as (aa, zz-old): 0 a->b, 100 b->a
    dp.book_pair("aa", "zz-old", 5)
    dp.flush()
    dp.book_pair("zz-old", "aa", 1)              # pending
    dp.rename_device("zz-old", "a0-new")
    dp.flush()
    got = {(r["a"], r["b"]): (r["ab"], r["ba"]) for r in dp.store.query("SELECT * FROM paths_pair_hour")}
    assert got == {("a0-new", "aa"): (101, 5)}                       # the new id sorts first: the two sums swap
    dp2 = make_paths()
    dp2.book_pair("x-old", "y-new", 9)           # becomes a conversation of the new id with itself: dropped
    dp2.rename_device("x-old", "y-new")
    dp2.flush()
    assert dp2.store.query("SELECT * FROM paths_pair_hour") == []


def test_ip_of_id():
    assert datapaths.ip_of_id("d-ip-192-168-0-28") == "192.168.0.28"
    assert datapaths.ip_of_id("d-ip-fd00--1") == "fd00::1"
    assert datapaths.ip_of_id("d-024c4c00001c") is None
    assert datapaths.ip_of_id("d-ip-nonsense") is None
    assert datapaths.ip_of_id(None) is None


# ---------------------------------------------------------------------------------------------- this host changes its id
def test_host_flows_follow_this_host_from_its_address_id_to_its_hardware_id(tmp_path):
    c = make_core(tmp_path)
    c.ingest({"hosts": [self_host(False)]}, None)
    assert c.self_device == OLD_ID
    host_flow(c)
    assert booked_ids(c) == {OLD_ID}
    host_flow(c, "5.5.5.5", 500, 0)               # booked on the old id, still pending when the id changes
    c.ingest({"hosts": [self_host(True)]}, None)  # the host's own interface now tells its hardware address
    assert c.self_device == SELF_ID
    assert booked_ids(c) == {SELF_ID}
    host_flow(c)
    assert booked_ids(c) == {SELF_ID}
    s = c.paths_summary("24h")
    devs = {d["id"]: d for d in s["devices"]}
    assert set(devs) == {SELF_ID} and devs[SELF_ID]["self"] and devs[SELF_ID]["countries"] == 2
    assert devs[SELF_ID]["tx"] == 2500 and devs[SELF_ID]["rx"] == 18000


def test_host_flows_use_the_self_entry_even_when_the_address_index_points_elsewhere(tmp_path):
    c = make_core(tmp_path)
    c.ingest({"hosts": [self_host(True)]}, None)
    c.ip_to_device[SELF_IP] = OLD_ID              # a stale address index must not decide where this host's bytes go
    host_flow(c)
    assert booked_ids(c) == {SELF_ID}


def test_merge_of_an_address_row_keeps_its_data_paths(tmp_path):
    c = make_core(tmp_path)
    c.ingest({"hosts": [self_host(True)]}, None)
    c.store.execute("UPDATE devices SET data=json_set(data, '$.is_self', json('false')) WHERE id=?", (SELF_ID,))
    c._refresh_index()
    h = self_host(False)
    h["is_self"] = False
    c.ingest({"hosts": [h]}, None)                 # once without the hardware address: an address row next to it
    assert c.store.one("SELECT id FROM devices WHERE id=?", (OLD_ID,))
    c.paths.book(OLD_ID, "3.3.3.3", 40, 60)
    c.paths.flush()
    h2 = self_host(True)
    h2["is_self"] = False
    c.ingest({"hosts": [h2]}, None)               # merged into the entry with the hardware address
    assert not c.store.one("SELECT id FROM devices WHERE id=?", (OLD_ID,))
    assert booked_ids(c) == {SELF_ID}


def test_placeholder_self_moves_to_the_host_entry_once_it_exists(tmp_path):
    c = make_core(tmp_path)
    c.self_ip = SELF_IP                           # known address, no entry yet: booked as "self"
    host_flow(c)
    assert booked_ids(c) == {"self"}
    c.ingest({"hosts": [self_host(True)]}, None)
    assert booked_ids(c) == {SELF_ID}


# ---------------------------------------------------------------------------------------------- leftovers of older versions
def test_start_moves_leftovers_once_and_keeps_a_copy(tmp_path):
    store = Store(str(tmp_path / "lanloom.db"))
    c = make_core(tmp_path, store)
    c.ingest({"hosts": [self_host(True)]}, None)
    hour = int(T0 // 3600)
    for dev, tx in ((OLD_ID, 1799), ("self", 5), ("d-ip-192-168-50-99", 3)):   # the last one matches no device: stays
        store.execute("INSERT INTO paths_hour(hour, device, cc, tx, rx) VALUES(?,?,?,?,?)", (hour, dev, "GB", tx, 0))
        store.execute("INSERT INTO paths_day(day, device, cc, tx, rx) VALUES(?,?,?,?,?)", (hour // 24, dev, "GB", tx, 0))
        store.execute("INSERT INTO paths_seen(device, cc, first, last) VALUES(?,?,?,?)", (dev, "GB", T0, T0))
    store.execute("INSERT INTO paths_hour(hour, device, cc, tx, rx) VALUES(?,?,?,?,?)", (hour, SELF_ID, "GB", 1, 1))

    c2 = make_core(tmp_path, store)               # the next start of the app
    got = {r["device"]: (r["tx"], r["rx"]) for r in store.query("SELECT device, tx, rx FROM paths_hour")}
    assert got == {SELF_ID: (1805, 1), "d-ip-192-168-50-99": (3, 0)}
    assert {r["device"] for r in store.query("SELECT device FROM paths_seen")} == {SELF_ID, "d-ip-192-168-50-99"}
    copy = store.get_setting(Core.REKEY_BACKUP)
    assert {(e["old"], e["new"]) for e in copy} == {(OLD_ID, SELF_ID), ("self", SELF_ID)}
    kept = next(e for e in copy if e["old"] == OLD_ID)["rows"]
    assert kept["paths_hour"] == [[hour, OLD_ID, "GB", 1799, 0]]

    assert c2.rekey_stale_paths() == 0            # idempotent: nothing left to move, the copy stays as it is
    make_core(tmp_path, store)
    assert store.get_setting(Core.REKEY_BACKUP) == copy


def test_copy_keeps_only_the_last_moves(tmp_path):
    store = Store(":memory:")
    c = make_core(tmp_path, store)
    c.ingest({"hosts": [self_host(True)]}, None)
    for i in range(Core.REKEY_KEEP + 3):
        store.execute("INSERT INTO paths_day(day, device, cc, tx, rx) VALUES(?,?,?,?,?)", (i, OLD_ID, "GB", 1, 0))
        assert c.rekey_stale_paths() == 1
    assert len(store.get_setting(Core.REKEY_BACKUP)) == Core.REKEY_KEEP


# ---------------------------------------------------------------------------------------------- never the raw id
def test_a_former_id_shows_its_address_and_is_marked():
    dp = make_paths()
    dp.book("d-ip-192-168-0-28", "3.3.3.3", 49, 0, now=T0)
    dp.book("d-gone0000001", "3.3.3.3", 48, 0, now=T0)
    s = dp.summary("24h", {}, now=T0)
    devs = {d["id"]: d for d in s["devices"]}
    assert devs["d-ip-192-168-0-28"]["gone"] and devs["d-ip-192-168-0-28"]["ip"] == "192.168.0.28"
    assert devs["d-gone0000001"]["gone"] and devs["d-gone0000001"]["ip"] is None
    v = dp.device_view("internet", "24h", {}, now=T0)
    assert all(d.get("gone") for d in v["devices"])


def test_view_never_falls_back_to_the_raw_id():
    js = (ROOT / "lanloom" / "web" / "js" / "paths.js").read_text()
    m = re.search(r"const devTitle = d => (.*);", js)
    assert m and "d.id)" not in m.group(1).replace("d.id ===", "") and "d.gone" in m.group(1)
    assert "|| r.id)" not in js and "|| u.device;" not in js
