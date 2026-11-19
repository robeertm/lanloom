"""Cities and network operators (DW1): the optional, streamed build of the compact file from the two source CSVs, the
checks before the swap, the fallback, the daily limit and the lookups. No real download: a fake fetch copies check
files made here from documentation address ranges (RFC 5737 / RFC 3849) and invented names."""
import csv
import gzip
import ipaddress
import time
import tracemalloc
from pathlib import Path

import pytest

from lanloom import geocity
from lanloom.geocity import CityData, CityFile, GeoCityError, Limits

NOW = time.mktime(time.strptime("2026-11-19 12:00:00", "%Y-%m-%d %H:%M:%S"))
SAMPLES = (("192.0.2.10", "XA", 64500, True), ("2001:db8::1", None, 64501, True))


def v4(a, b):
    return str(ipaddress.ip_address(a)), str(ipaddress.ip_address(b))


def write_gz(path: Path, rows) -> Path:
    with gzip.open(path, "wt", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


def asn_rows():
    return [
        ["192.0.2.0", "192.0.2.255", "64500", "Example Transit Ltd"],
        ["198.51.100.0", "198.51.100.127", "64502", "Example Broadband, Inc."],
        ["198.51.100.128", "198.51.100.255", "64502", "Example Broadband, Inc."],
        ["203.0.113.0", "203.0.113.255", "64503", "Sample Cloud"],
        ["2001:db8::", "2001:db8:0:ffff:ffff:ffff:ffff:ffff", "64501", "Example Six"],
        ["2001:db8:1::", "2001:db8:1:0:ffff:ffff:ffff:ffff", "64501", "Example Six"],
    ]


def city_rows():
    return [
        ["192.0.2.0", "192.0.2.127", "EU", "XA", "Northern District", "Alphaville", "50.10", "8.60"],
        ["192.0.2.128", "192.0.2.255", "EU", "XA", "Northern District", "Alphaville", "50.10", "8.60"],
        ["198.51.100.0", "198.51.100.63", "EU", "XB", "Coast", "Betatown", "-33.90", "151.20"],
        # a gap from 198.51.100.64 to 198.51.100.255, then a range the source marks as unusable
        ["203.0.113.0", "203.0.113.255", "ZZ", "ZZ", "", "", "0", "0"],
        ["2001:db8::", "2001:db8:0:ffff:ffff:ffff:ffff:ffff", "EU", "XA", "Northern District", "Alphaville", "50.10", "8.60"],
        ["2001:db8:1::", "2001:db8:1:0:ffff:ffff:ffff:ffff", "AS", "XC", "", "Gammaport", "35.60", "139.70"],
    ]


class Fake:
    """A fetch that copies prepared files; counts the calls."""

    def __init__(self, tmp: Path, asn=None, city=None):
        self.asn = asn or write_gz(tmp / "src-asn.csv.gz", asn_rows())
        self.city = city or write_gz(tmp / "src-city.csv.gz", city_rows())
        self.calls = []

    def __call__(self, url, dest, max_bytes, progress=None):
        self.calls.append(url)
        src = self.asn if "-asn-" in url else self.city
        dest.write_bytes(src.read_bytes())
        if progress:
            progress(dest.stat().st_size, dest.stat().st_size)


def make(tmp_path, fake=None, **kw):
    folder = tmp_path / "geo"
    data = CityData(folder, fetch=fake or Fake(tmp_path), clock=lambda: NOW, limits=Limits.small(SAMPLES), min_free=1, **kw)
    return data


def test_build_lookup_and_names(tmp_path):
    d = make(tmp_path)
    d.configure(True, background=False)
    assert d.step(NOW) is True
    assert d.file.month == "2026-11"
    idx, asn = d.lookup("192.0.2.200")
    assert asn == 64500 and d.city(idx)["name"] == "Alphaville" and d.city(idx)["cc"] == "XA"
    assert d.city(idx)["key"] == "Alphaville|Northern District"
    assert d.operator(64500) == "Example Transit Ltd"
    assert d.operator(64502) == "Example Broadband, Inc."          # a comma inside the name survives
    # ranges that follow each other with the same city or operator are merged
    assert len(d.file.s["c4s"]) < 6
    # a gap and a range of unusable data have no city; the operator may still be known
    assert d.lookup("198.51.100.200") == (0, 64502)
    assert d.lookup("203.0.113.5") == (0, 64503)
    assert d.lookup("198.18.0.1") == (0, 0)
    # IPv6 is cut to the upper 64 bits
    i6, a6 = d.lookup("2001:db8:1:0:1234::1")
    assert a6 == 64501 and d.city(i6)["name"] == "Gammaport"
    assert d.lookup("::ffff:192.0.2.10") == d.lookup("192.0.2.10")
    assert d.lookup("not an address") == (0, 0)
    assert d.city(0) is None and d.city(10 ** 6) is None and d.operator(1) is None
    info = d.info()
    assert info["status"] == "ready" and info["cities"] == 3 and info["operators"] == 4
    assert info["attribution"]["text"] == "IP Geolocation by DB-IP"


def test_off_by_default_costs_nothing(tmp_path):
    fake = Fake(tmp_path)
    d = make(tmp_path, fake)
    assert d.info()["status"] == "off"
    assert d.lookup("192.0.2.1") == (0, 0)
    assert d.step(NOW) is False and fake.calls == []                  # switched off: no connection at all
    d.configure(True, background=False)
    d.configure(False, background=False)
    assert d.step(NOW) is False and d.info()["status"] == "off"
    assert d.lookup("192.0.2.1") == (0, 0)


def test_never_available_without_a_folder():
    d = CityData(None)
    d.configure(True)
    assert d.enabled is False and d.info()["status"] == "unavailable"


def test_wanted_month_and_daily_limit(tmp_path):
    assert geocity.wanted_month(time.mktime(time.strptime("2026-11-01 09:00", "%Y-%m-%d %H:%M"))) == "2026-10"
    assert geocity.wanted_month(time.mktime(time.strptime("2026-11-02 09:00", "%Y-%m-%d %H:%M"))) == "2026-11"
    assert geocity.wanted_month(time.mktime(time.strptime("2027-01-01 09:00", "%Y-%m-%d %H:%M"))) == "2026-12"
    fake = Fake(tmp_path)
    d = make(tmp_path, fake)
    d.configure(True, background=False)
    assert d.step(NOW) is True and len(fake.calls) == 2              # one file each: operators and cities
    assert d.step(NOW + 100) is False and len(fake.calls) == 2       # current month is there: no more requests
    # next month, the check happens once a day even when it fails
    bad = Fake(tmp_path, city=write_gz(tmp_path / "bad.csv.gz", []))
    d.fetch = bad
    later = NOW + 40 * 86400
    d.clock = lambda: later
    assert d.step(later) is False and d.info()["status"] == "ready"  # the old file stays in use
    n = len(bad.calls)
    assert d.step(later + 3600) is False and len(bad.calls) == n     # at most one check a day
    assert d.due(later + 86401 + 1) is not None
    assert d.refresh_now() is True and d.due(later + 10) is not None


def test_daily_check_survives_a_restart(tmp_path):
    fake = Fake(tmp_path, city=write_gz(tmp_path / "bad.csv.gz", []))
    d = make(tmp_path, fake)
    d.configure(True, background=False)
    assert d.step(NOW) is False and fake.calls
    d2 = make(tmp_path, fake)
    d2.configure(True, background=False)
    n = len(fake.calls)
    assert d2.step(NOW + 60) is False and len(fake.calls) == n


@pytest.mark.parametrize("mutate,reason", [
    ("truncated", "gzip"),
    ("unsorted", "ascending"),
    ("bad_row", "city row"),
    ("bad_coord", "coordinates"),
    ("empty", "too few rows"),
    ("sample", "sample"),
])
def test_broken_sources_never_replace_a_good_file(tmp_path, mutate, reason):
    d = make(tmp_path)
    d.configure(True, background=False)
    assert d.step(NOW) is True
    good = sorted(p.name for p in (tmp_path / "geo").glob("cities-*.bin"))
    rows = city_rows()
    if mutate == "truncated":
        raw = (tmp_path / "src-city.csv.gz").read_bytes()
        src = tmp_path / "cut.csv.gz"
        src.write_bytes(raw[:-12])
    elif mutate == "unsorted":
        src = write_gz(tmp_path / "x.csv.gz", [rows[2], rows[0]])
    elif mutate == "bad_row":
        src = write_gz(tmp_path / "x.csv.gz", [["192.0.2.0", "oops", "EU", "XA", "", "A", "1", "1"]])
    elif mutate == "bad_coord":
        src = write_gz(tmp_path / "x.csv.gz", [["192.0.2.0", "192.0.2.9", "EU", "XA", "", "A", "99999", "1"]])
    elif mutate == "empty":
        src = write_gz(tmp_path / "x.csv.gz", [])
    else:
        rows[0][3] = "XZ"
        src = write_gz(tmp_path / "x.csv.gz", rows)
    d.fetch = Fake(tmp_path, city=src)
    d.clock = lambda: NOW + 40 * 86400
    ok = d.step(NOW + 40 * 86400)
    assert ok is False
    assert reason in (d.info()["error"] or "") or d.info()["status"] == "ready"
    assert sorted(p.name for p in (tmp_path / "geo").glob("cities-*.bin")) == good
    assert not list((tmp_path / "geo").glob("*.tmp")) and not list((tmp_path / "geo").glob("build-*"))
    assert d.lookup("192.0.2.200")[1] == 64500                      # still answering from the old file


def test_download_size_window(tmp_path):
    d = make(tmp_path)
    d.limits = Limits(city_rows=1, asn_rows=1, v6_runs=0, city_size=(10_000_000, 20_000_000), asn_size=(1, 10_000_000), samples=())
    d.configure(True, background=False)
    assert d.step(NOW) is False
    assert "size" in d.info()["error"] and d.file is None and d.info()["status"] == "error"


def test_previous_month_when_the_current_one_is_missing(tmp_path):
    class Fetch(Fake):
        def __call__(self, url, dest, max_bytes, progress=None):
            if "2026-11" in url:
                raise GeoCityError("http 404")
            super().__call__(url, dest, max_bytes, progress)
    d = make(tmp_path, Fetch(tmp_path))
    d.configure(True, background=False)
    assert d.step(NOW) is True and d.file.month == "2026-10"


def test_fallback_and_pruning_of_old_files(tmp_path):
    d = make(tmp_path)
    d.configure(True, background=False)
    for months in (0, 40, 70, 100):
        t = NOW + months * 86400
        d.clock = lambda t=t: t
        d.state["last_check"] = None
        d.step(t)
    files = sorted(p.name for p in (tmp_path / "geo").glob("cities-*.bin"))
    assert len(files) == geocity.KEEP_FILES                          # the newest plus one fallback
    # the newest file is damaged: the next start maps the one before
    newest = tmp_path / "geo" / files[-1]
    newest.write_bytes(b"garbage")
    d2 = make(tmp_path)
    d2.configure(True, background=False)
    assert d2.file is not None and d2.file.path.name == files[0]


def test_free_space_is_checked_before_anything_is_fetched(tmp_path):
    fake = Fake(tmp_path)
    d = CityData(tmp_path / "geo", fetch=fake, clock=lambda: NOW, limits=Limits.small(SAMPLES), min_free=1 << 62)
    d.configure(True, background=False)
    assert d.step(NOW) is False and fake.calls == [] and "space" in d.info()["error"]


def test_only_the_source_host_is_ever_used(tmp_path):
    with pytest.raises(GeoCityError):
        geocity.http_download("https://example.net/x.csv.gz", tmp_path / "x", 10)
    with pytest.raises(GeoCityError):
        geocity.http_download("http://" + geocity.HOST + "/free/x.csv.gz", tmp_path / "x", 10)


def test_replaced_file_keeps_the_old_mapping_valid(tmp_path):
    d = make(tmp_path)
    d.configure(True, background=False)
    d.step(NOW)
    old = d.file
    gen = d.generation
    later = NOW + 40 * 86400
    d.clock = lambda: later
    d.state["last_check"] = None
    assert d.step(later) is True and d.generation > gen and d.file is not old
    assert d.lookup("192.0.2.200")[1] == 64500


def test_clear_removes_the_files(tmp_path):
    d = make(tmp_path)
    d.configure(True, background=False)
    d.step(NOW)
    d.clear()
    assert d.file is None and not list((tmp_path / "geo").glob("cities-*.bin"))
    assert d.lookup("192.0.2.200") == (0, 0)


def test_header_damage_is_refused(tmp_path):
    d = make(tmp_path)
    d.configure(True, background=False)
    d.step(NOW)
    p = d.file.path
    raw = p.read_bytes()
    for bad in (raw[:len(raw) // 2], b"LLCITY1\n" + b"\0\0\0\5{bad}", raw[:40]):
        q = tmp_path / "bad.bin"
        q.write_bytes(bad)
        with pytest.raises(GeoCityError):
            CityFile(q)


def test_memory_of_a_build_stays_small(tmp_path):
    """60,000 ranges with 2,000 distinct cities: the arrays are spilled, only the table of distinct cities is held."""
    rows_c, rows_a = [], []
    base = int(ipaddress.ip_address("10.0.0.0"))                     # synthetic, private: never looked up here
    for i in range(60_000):
        a = base + i * 4
        s, e = str(ipaddress.ip_address(a)), str(ipaddress.ip_address(a + 3))
        rows_c.append([s, e, "EU", "XA", "R%d" % (i % 50), "City%d" % (i % 2000), "10.0", "20.0"])
        rows_a.append([s, e, str(64000 + i % 3000), "Operator %d" % (i % 3000)])
    c = write_gz(tmp_path / "c.csv.gz", rows_c)
    a = write_gz(tmp_path / "a.csv.gz", rows_a)
    d = CityData(tmp_path / "geo", fetch=Fake(tmp_path, asn=a, city=c), clock=lambda: NOW,
                 limits=Limits.small(), min_free=1)
    d.configure(True, background=False)
    tracemalloc.start()
    ok = d.step(NOW)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert ok is True
    assert peak < 25 * 1024 * 1024, peak
    idx, asn = d.lookup("10.0.0.5")
    assert d.city(idx)["name"].startswith("City") and asn >= 64000
    # lookups do not grow the heap
    tracemalloc.start()
    for i in range(20_000):
        d.lookup("10.0.%d.%d" % (i % 200, i % 250))
    _, peak2 = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak2 < 1024 * 1024, peak2
