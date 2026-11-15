"""Data paths: offline geo data, buckets (country / own / unassigned), periods, EU share, unusual behaviour, API."""
import gzip
import ipaddress
import json
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod, datapaths, geo
from lanloom.main import create_app
from lanloom.store import Store

ROOT = Path(__file__).resolve().parent.parent
HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
CGNAT_HOST = str(ipaddress.ip_network("100.64.0.0/10")[77])        # overlay VPN space (written as a network on purpose)
T0 = 1_790_000_000.0                                                # fixed clock: no test depends on the wall clock


# ---------------------------------------------------------------------- geo data
@pytest.fixture(scope="module")
def real_geo():
    g = geo.Geo()
    g.start(background=False)
    assert g.db is not None, g.error
    return g


def test_bundled_file_loads_and_answers(real_geo):
    assert real_geo.country("8.8.8.8") == "US"
    assert real_geo.country("::ffff:8.8.8.8") == "US"
    assert real_geo.country("10.1.2.3") == geo.RESERVED
    assert real_geo.country("fd00::1") == geo.RESERVED
    assert re.fullmatch(r"[A-Z]{2}", real_geo.country("2001:4860:4860::8888"))
    assert real_geo.country("not-an-address") is None
    info = real_geo.info()
    assert info["loaded"] and re.fullmatch(r"\d{4}-\d{2}", info["month"]) and info["rows"] > 300_000
    assert info["attribution"]["text"] == "IP Geolocation by DB-IP" and info["attribution"]["license"] == "CC BY 4.0"
    assert geo.DATA_FILE.stat().st_size < 3_000_000


def _tiny(n=1200):
    """Synthetic table: n IPv4 and n IPv6 starts, alternating between two countries."""
    v4 = [i * (2 ** 32 // n) for i in range(n)]
    v6 = [i * (2 ** 64 // n) for i in range(n)]
    idx = [1 + (i % 2) for i in range(n)]
    idx[0] = 0
    return geo.pack({"codes": ["ZZ", "AA", "BB"], "month": "2000-01"}, v4, idx, v6, idx)


def test_pack_roundtrip_and_lookup():
    db = geo.GeoDB(_tiny())
    assert db.country("0.0.0.1") == "ZZ"
    step = 2 ** 32 // 1200
    assert db.country(str(ipaddress.ip_address(step + 5))) == "BB"
    assert db.country(str(ipaddress.ip_address(2 * step + 5))) == "AA"
    assert db.country("255.255.255.255") in ("AA", "BB")
    assert db.month == "2000-01"


@pytest.mark.parametrize("damage", ["magic", "truncated", "codes", "gzip"])
def test_broken_file_is_refused(tmp_path, damage):
    raw = _tiny()
    if damage == "magic":
        raw = b"XXGEO1\n" + raw[7:]
    elif damage == "truncated":
        raw = raw[:-100]
    elif damage == "codes":
        raw = raw.replace(b'"ZZ"', b'"Z1"', 1)
    p = tmp_path / "geo.bin.gz"
    p.write_bytes(raw[: len(raw) // 2] if damage == "gzip" else gzip.compress(raw))
    if damage == "gzip":
        p.write_bytes(gzip.compress(raw)[:500])
    with pytest.raises(geo.GeoError):
        geo.GeoDB.from_file(p)
    g = geo.Geo(p)
    g.start(background=False)
    assert g.db is None and g.error and g.info()["loaded"] is False


def test_build_tool_checks_its_input(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_geodb
    monkeypatch.setattr(build_geodb, "MIN_ROWS", {4: 3, 6: 3})
    good = ["0.0.0.0,0.255.255.255,ZZ", "1.0.0.0,8.8.255.255,AU", "8.9.0.0,255.255.255.255,US",
            "::,1fff:ffff:ffff:ffff:ffff:ffff:ffff:ffff,ZZ", "2000::,ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff,DE"]
    p = tmp_path / "dbip-country-lite-2000-01.csv.gz"
    p.write_bytes(gzip.compress("\n".join(good).encode()))
    codes, fam = build_geodb.read(p)
    assert codes[0] == "ZZ" and "AU" in codes and len(fam[4][0]) == 3
    gap = list(good)
    gap[1] = "1.0.0.1,8.8.255.255,AU"
    p.write_bytes(gzip.compress("\n".join(gap).encode()))
    with pytest.raises(SystemExit, match="gaps"):
        build_geodb.read(p)
    p.write_bytes(gzip.compress("\n".join(good[:-1]).encode()))
    with pytest.raises(SystemExit, match="end of the address space"):
        build_geodb.read(p)
    p.write_bytes(gzip.compress(b"1.0.0.0,1.0.0.255,XYZ"))
    with pytest.raises(SystemExit):
        build_geodb.read(p)


# ---------------------------------------------------------------------- bookkeeping
class FakeGeo:
    def __init__(self, table=None, loaded=True):
        self.table = table or {"8.8.8.8": "US", "193.0.0.1": "DE", "198.51.100.9": "ZZ", "203.0.113.5": "ZZ"}
        self.db = object() if loaded else None

    def country(self, ip):
        return self.table.get(ip, "FR")

    def info(self):
        return {"loaded": self.db is not None}


def make(settings=None, loaded=True, now=T0):
    st = Store(":memory:")
    clock = {"t": now}
    s = {"home_country": "DE", "paths_own_networks": [], "paths_no_internet": []}
    s.update(settings or {})
    dp = datapaths.DataPaths(st, FakeGeo(loaded=loaded), lambda: s, clock=lambda: clock["t"])
    dp.clock_ref = clock
    return dp


def test_buckets_country_own_and_tunnel():
    dp = make({"paths_own_networks": ["198.18.0.0/15"]})
    assert dp.classify("8.8.8.8") == "US"
    assert dp.classify(CGNAT_HOST) == datapaths.OWN             # overlay VPN: own, never unassigned
    assert dp.classify("198.18.4.4") == datapaths.OWN            # listed own network
    assert dp.classify("203.0.113.5") == datapaths.OWN           # special range of the geo data
    assert dp.classify("::ffff:8.8.8.8") == "US"
    assert dp.classify("nonsense") is None


def test_without_geo_data_nothing_is_booked():
    dp = make(loaded=False)
    assert dp.book("dev", "8.8.8.8", 5000, 5000) is False
    dp.flush()
    assert dp.unbooked == 10000
    s = dp.summary("24h", {})
    assert s["totals"]["attributable"] == 0 and s["totals"]["unassigned"] == 0
    assert s["totals"]["outside_eu_share"] is None


def test_share_refers_to_attributable_traffic_only():
    dp = make()
    dp.book("dev", CGNAT_HOST, 49_000_000_000, 0)          # the lesson: 49 GB through a tunnel ...
    dp.book("dev", "193.0.0.1", 500_000_000, 0)             # ... 0.5 GB in the EU
    dp.book("dev", "8.8.8.8", 500_000_000, 0)               # ... 0.5 GB outside
    tt = dp.summary("24h", {"dev": {"title": "Laptop", "ip": "192.168.50.20"}})["totals"]
    assert tt["own"] == 49_000_000_000
    assert tt["attributable"] == 1_000_000_000
    assert tt["outside_eu_share"] == pytest.approx(0.5)
    dp2 = make()
    dp2.book("dev", CGNAT_HOST, 10_000, 10_000)
    assert dp2.summary("24h", {})["totals"]["outside_eu_share"] is None       # nothing attributable: no data, not 0 %


def test_windows_hours_and_days_and_pruning():
    dp = make()
    now = T0
    for hours_ago in (1, 30, 24 * 5, 24 * 20, 24 * 40):
        dp.book("dev", "8.8.8.8", 1000, 0, now - hours_ago * 3600)
    dp.flush(now)
    sums = {w: dp.summary(w, {}, now)["totals"]["tx"] for w in ("24h", "7d", "30d")}
    assert sums["24h"] == 1000
    assert sums["7d"] in (2000, 3000)                       # day buckets: today + 6 days back (30 h ago may fall on day -1 or -2)
    assert 3000 <= sums["30d"] <= 4000 and sums["30d"] > sums["7d"]
    assert dp.store.one("SELECT COUNT(*) AS n FROM paths_hour WHERE hour < ?", (int(now // 3600) - datapaths.KEEP_HOURS,))["n"] == 0
    assert dp.store.one("SELECT COUNT(*) AS n FROM paths_day WHERE day < ?", (int(now // 86400) - datapaths.KEEP_DAYS,))["n"] == 0


def test_no_addresses_are_stored():
    dp = make()
    dp.book("d1", "8.8.8.8", 10, 20)
    dp.flush()
    dump = json.dumps([dp.store.query("SELECT * FROM %s" % t) for t in ("paths_hour", "paths_day", "paths_seen")])
    assert "8.8.8.8" not in dump and "US" in dump


def test_new_country_after_warmup_only():
    dp = make()
    t = dp.clock_ref
    dp.book("tv", "193.0.0.1", 10_000_000, 0, T0 - 5 * 86400)       # history starts five days ago
    dp.flush(T0 - 5 * 86400)
    dp.book("tv", "8.8.8.8", 2_000_000, 0, T0 - 3600)               # new country, 2 MB
    dp.book("fresh", "193.0.0.1", 9_000_000, 0, T0 - 7200)          # a device seen for the first time today ...
    dp.book("fresh", "8.8.8.8", 9_000_000, 0, T0 - 3600)            # ... is not "new country" yet (warm-up)
    dp.book("tv", "198.51.100.9", 9_000_000, 0, T0 - 3600)          # own range never counts as a country
    t["t"] = T0
    un = dp.summary("24h", {})["unusual"]
    assert [(u["kind"], u["device"], u.get("cc")) for u in un] == [("new_country", "tv", "US")]


def test_heavy_upload_against_typical_day():
    dp = make()
    for d in range(1, 8):
        dp.book("nas", "193.0.0.1", 100_000_000, 0, T0 - d * 86400 - 7200)
    dp.book("nas", "193.0.0.1", 450_000_000, 0, T0 - 3600)
    dp.book("quiet", "193.0.0.1", 150_000_000, 0, T0 - 3600)        # below 200 MB: never heavy
    un = dp.summary("24h", {}, T0)["unusual"]
    heavy = [u for u in un if u["kind"] == "heavy"]
    assert len(heavy) == 1 and heavy[0]["device"] == "nas" and heavy[0]["typical"] == 100_000_000


def test_network_without_internet_talks_anyway():
    dp = make({"paths_no_internet": ["192.168.50.48/28"]})
    dp.book("cam", "8.8.8.8", 3000, 100, T0 - 60)
    dp.book("cam2", CGNAT_HOST, 3000, 100, T0 - 60)                 # through an own tunnel: not the internet
    devices = {"cam": {"ip": "192.168.50.50"}, "cam2": {"ip": "192.168.50.51"}}
    un = dp.summary("24h", devices, T0)["unusual"]
    assert [(u["kind"], u["device"], u["network"]) for u in un] == [("offline_net", "cam", "192.168.50.48/28")]


def test_forget_device_removes_its_rows():
    dp = make()
    dp.book("a", "8.8.8.8", 1, 1)
    dp.book("b", "8.8.8.8", 1, 1)
    dp.flush()
    dp.forget_device("a")
    assert {r["device"] for r in dp.store.query("SELECT device FROM paths_hour")} == {"b"}


@pytest.mark.parametrize("bad", [["10.0.0.0/33"], ["nonsense"], "10.0.0.0/8", [""], ["1.2.3.0/24"] * 0 + [5]])
def test_parse_networks_rejects(bad):
    assert datapaths.parse_networks(bad) is None


def test_parse_networks_normalises():
    assert datapaths.parse_networks(["192.168.50.7/24", " 2001:db8::/32 ", "192.168.50.0/24"]) == ["192.168.50.0/24", "2001:db8::/32"]


# ---------------------------------------------------------------------- core + API
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        c.anon = TestClient(app, base_url="http://lanloom.test")
        assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
        yield c


def test_api_needs_a_session(client):
    assert client.anon.get("/api/paths").status_code == 401


def test_api_demo_summary(client):
    for _ in range(50):
        if client.get("/api/paths").json()["geo"]["loaded"]:
            break
        time.sleep(0.1)
    for w in ("24h", "7d", "30d"):
        d = client.get("/api/paths?window=" + w).json()
        assert d["window"] == w and d["home"] == "NL"
        assert d["totals"]["countries"] >= 10 and d["totals"]["devices"] >= 10
        assert 0 < d["totals"]["outside_eu_share"] < 1
        assert {c["cc"] for c in d["countries"]} >= {"US", "NL", "CN"}
        assert all(not c["cc"].startswith("~") for c in d["countries"])
        assert {u["kind"] for u in d["unusual"]} == {"new_country", "heavy", "offline_net"}
    assert client.get("/api/paths?window=bogus").json()["window"] == "24h"


def test_settings_validation_all_or_nothing(client):
    r = client.put("/api/settings", json={"home_country": "fr", "paths_own_networks": ["198.51.100.0/24"]}, headers=HDR)
    assert r.status_code == 200 and r.json()["home_country"] == "FR" and r.json()["paths_own_networks"] == ["198.51.100.0/24"]
    r = client.put("/api/settings", json={"home_country": "DE", "paths_no_internet": ["192.168.50.0/99"]}, headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "settings.paths.invalid_network"
    assert client.get("/api/settings").json()["home_country"] == "FR"          # nothing of the refused patch was written
    r = client.put("/api/settings", json={"home_country": "Germany"}, headers=HDR)
    assert r.json()["home_country"] == "FR"


def test_host_flows_feed_data_paths(tmp_path):
    from types import SimpleNamespace
    from lanloom.core import Core
    cfg = SimpleNamespace(data_dir=tmp_path, demo=False, probe=True)
    c = Core(cfg, Store(":memory:"), None)
    c.geo.start(background=False)
    c.on_event("host", None, {"type": "bytes", "flows": [
        {"local_ip": "192.168.50.11", "peer_ip": "8.8.8.8", "peer_port": 443, "tx": 1000, "rx": 9000},
        {"local_ip": "192.168.50.11", "peer_ip": "192.168.50.10", "peer_port": 445, "tx": 5000, "rx": 5000},   # LAN: not a data path
        {"local_ip": "192.168.50.11", "peer_ip": CGNAT_HOST, "peer_port": 41641, "tx": 700, "rx": 300}]})
    tt = c.paths_summary("24h")["totals"]
    assert tt["attributable"] == 10_000 and tt["own"] == 1000 and tt["countries"] == 1


# ---------------------------------------------------------------------- texts, attribution, assets
def test_every_text_key_of_the_view_exists_in_both_languages():
    js = (ROOT / "lanloom/web/js/paths.js").read_text()
    keys = {k for k in re.findall(r"\bt\('([a-z0-9_.]+)'", js) if not k.endswith('.')}
    keys |= {"paths.unusual.kind." + k for k in ("new_country", "heavy", "offline_net")}
    keys |= {"paths.window." + w for w in ("24h", "7d", "30d")}
    for lang in ("en", "de"):
        strings = json.loads((ROOT / ("lanloom/i18n/%s.json" % lang)).read_text())
        assert keys <= set(strings), (lang, keys - set(strings))


def test_attribution_everywhere():
    for rel in ("README.md", "THIRD-PARTY-NOTICES.md", "lanloom/web/js/paths.js", "lanloom/geo.py"):
        assert "IP Geolocation by DB-IP" in (ROOT / rel).read_text(), rel
    assert "Natural Earth" in (ROOT / "THIRD-PARTY-NOTICES.md").read_text()


def test_assets_are_loaded_and_need_no_network():
    html = (ROOT / "lanloom/web/index.html").read_text()
    assert html.index("js/worldmap.js") < html.index("js/paths.js") and "css/paths.css" in html
    js = (ROOT / "lanloom/web/js/paths.js").read_text()
    assert "fetch(" not in js and "http://" not in js.replace("http://www.w3.org/2000/svg", "")
    for src in ("lanloom/geo.py", "lanloom/datapaths.py"):
        text = (ROOT / src).read_text()
        assert "urllib" not in text and "socket" not in text and "requests" not in text
