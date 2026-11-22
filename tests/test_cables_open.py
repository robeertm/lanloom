"""Real cables, open sources: the second cable file (public domain / CC0), its build from the national submarine cable corridors
and the cable facts of the open knowledge base, the merge rules and the run time.

All names of cables, owners and places in the synthetic tests are invented; the corridors lie on an empty sea."""
import gzip
import hashlib
import importlib.util
import io
import json
import re
import sqlite3
import struct
import zipfile
from pathlib import Path

import pytest

from lanloom import cableroute, cables, cables_open

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("lanloom_build_cables_open", ROOT / "scripts" / "build_cables.py")
build_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_script)


# ---------------------------------------------------------------------------------------------------------- helpers
def line(lon0, lat0, lon1, lat1, n=12):
    return [(lon0 + (lon1 - lon0) * i / n, lat0 + (lat1 - lat0) * i / n) for i in range(n + 1)]


def corridor(pts, w=0.0003):
    """A thin closed ring around a line (both sides w degrees apart), like the corridors of the national data."""
    left = [(x, y + w) for x, y in pts]
    right = [(x, y - w) for x, y in pts][::-1]
    ring = left + right
    return ring + [ring[0]]


def wkb_multipolygon(rings):
    b = struct.pack("<BII", 1, 6, len(rings))
    for r in rings:
        b += struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(r)) + b"".join(struct.pack("<dd", x, y) for x, y in r)
    return b


def gpkg_blob(rings):
    return b"GP" + bytes([0, 0b00000001]) + struct.pack("<i", 4269) + wkb_multipolygon(rings)      # little endian, no envelope


def gpkg_file(path, rows):
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT, identifier TEXT);
      CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, geometry_type_name TEXT, srs_id INT, z INT, m INT);
      CREATE TABLE Cables (OBJECTID INTEGER PRIMARY KEY, spatial BLOB, shortName TEXT, cableSystem TEXT, owner TEXT, status TEXT, region TEXT);
      INSERT INTO gpkg_contents VALUES ('Cables', 'features', 'Cables');
      INSERT INTO gpkg_geometry_columns VALUES ('Cables', 'spatial', 'MULTIPOLYGON', 4269, 0, 0);""")
    for name, owner, status, rings in rows:
        con.execute("INSERT INTO Cables (spatial, shortName, cableSystem, owner, status, region) VALUES (?, NULL, ?, ?, ?, 'Test')",
                    (gpkg_blob(rings), name, owner, status))
    con.commit()
    con.close()


def binding(qid, label, aliases="", owners="", ops="", inception="", service="", length="", cap="", unit="", cc="", ended=""):
    b = {"i": {"value": "http://www.wikidata.org/entity/" + qid}, "label": {"value": label}}
    for k, v in (("aliases", aliases), ("owners", owners), ("ops", ops), ("inception", inception), ("service", service),
                 ("len", length), ("cap", cap), ("capUnit", unit and "http://www.wikidata.org/entity/" + unit), ("cc", cc), ("ended", ended)):
        if v:
            b[k] = {"value": v}
    return b


def sparql(*bindings):
    return json.dumps({"head": {"vars": []}, "results": {"bindings": list(bindings)}}).encode()


def base_set(*named):
    ways = [{"id": i + 1, "n": n, "ref": None, "op": op, "sea": True, "pts": line(-40, 10 + i, -10, 10 + i)} for i, (n, op) in enumerate(named)]
    return cables.build(ways, "2026-11-22")


# ---------------------------------------------------------------------------------------------------------- the bundled file
def test_bundled_open_file_passes_its_checks_checksum_and_size_budget():
    blob = cables_open.DATA_FILE.read_bytes()
    digest, name = (cables_open.DATA_FILE.parent / "cables-open.sha256").read_text(encoding="ascii").split()
    assert name == cables_open.DATA_FILE.name and hashlib.sha256(blob).hexdigest() == digest, "edited by hand? rebuild with scripts/build_cables.py"
    assert len(blob) <= build_script.MAX_OPEN_GZ and len(gzip.decompress(blob)) <= build_script.MAX_OPEN_RAW
    d = cables_open.decode(blob)
    assert cables_open.problems(d) == [] and build_script.samples_open(d) == []
    assert {s["id"] for s in d["sources"]} == {"noaa", "wikidata"}
    assert all(s["license"] == "CC0 1.0" and s["license_url"].startswith("https://creativecommons.org/publicdomain/zero/") for s in d["sources"])
    for e in d["sea"]:
        assert e["src"] == "noaa" and re.match(r"^n\d+$", e["id"]) and e["n"]
        assert set(e) <= {"id", "n", "op", "d", "km", "ends", "src", "part"}
    assert d["counts"]["sea"] == len(d["sea"]) and d["counts"]["facts"] == len(d["meta"])


def test_bundled_open_file_holds_no_line_of_a_cable_the_community_map_has():
    base = cables.decode(cables.DATA_FILE.read_bytes())
    d = cables_open.decode(cables_open.DATA_FILE.read_bytes())
    have, idx = cables_open._base_keys(base, d["meta"])
    for e in d["sea"]:
        assert not (cables_open.name_keys(e["n"]) & have["keys"]), e["n"]
        m = cables_open.lookup(idx, e["n"])
        assert not (m and m["wd"] in have["wd"]), e["n"]


def test_owners_of_the_bundled_file_name_companies_not_persons():
    d = cables_open.decode(cables_open.DATA_FILE.read_bytes())
    for e in d["sea"]:
        for part in (e.get("op") or "").split(", "):
            words = part.split()
            assert not (2 <= len(words) <= 3 and all(re.match(r"^[A-Z][a-z]+$", w) for w in words)
                        and not cables_open._COMPANY.search(part)), part


def test_licence_file_and_notices_name_the_open_sources():
    lic = (ROOT / "lanloom" / "data" / "CABLES-LICENSE.md").read_text(encoding="utf-8")
    for want in ("cables-open.json.gz", "CC0 1.0", "NOAA Office for Coastal Management", "BOEM", "Wikidata", "public domain",
                 "cables-open.sha256", "the part in U.S. waters"):
        assert want in lic, want
    notes = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
    row = next(l for l in notes.splitlines() if "cables-open.json.gz" in l)
    assert "CC0" in row and "NOAA" in row and "Wikidata" in row


# ---------------------------------------------------------------------------------------------------------- names and facts
def test_name_keys_ignore_case_punctuation_filler_words_and_brackets():
    assert cables_open.name_keys("Sea-Link 3 Cable System") == cables_open.name_keys("SEALINK-3") == {"sealink3"}
    assert cables_open.name_keys("Bay Fibre (North Crossing)") == {"baynorthcrossing", "bay"}
    assert "transpacificexpress" in cables_open.name_keys("Trans-Pacific Express")
    assert cables_open.name_keys("Trans-Pacific Express") != cables_open.name_keys("Trans-Pacific Cable")
    assert cables_open.name_keys("Cable") == set() and cables_open.name_keys(None) == set() and cables_open.name_keys("  ") == set()
    assert cables_open.name_keys("Ébène & Île") == {"ebeneandile"}


def test_facts_are_read_with_units_aliases_owners_and_the_year_out_of_service():
    meta = cables_open.meta_from_answer(sparql(
        binding("Q11", "Alpha Link", aliases="AlphaLink 1|AL-1", owners="North Telco|South Carrier Ltd|", inception="1999-03-01T00:00:00Z",
                service="2001-06-01T00:00:00Z", length="6400000", cap="72", unit="Q1084321", cc="xa|XB|zzz"),
        binding("Q12", "Beta Link", ops="Beta Operator", cap="560", unit="Q2243141", ended="2010-01-01T00:00:00Z"),
        binding("Q13", "Gamma", cap="5", unit="Q9999"),
        binding("nonsense", "No Item"), {"label": {"value": "No id"}}))
    a, b, g = meta
    assert a["wd"] == "Q11" and a["own"] == "North Telco, South Carrier Ltd" and a["yr"] == 2001 and a["km"] == 6400
    assert a["cap"] == 72.0 and a["cc"] == ["XA", "XB"] and {"alphalink", "alphalink1", "al1"} <= set(a["k"]) and a["end"] is None
    assert b["own"] == "Beta Operator" and b["cap"] == 0.56 and b["end"] == 2010
    assert g["cap"] is None                                          # an unknown unit is never guessed
    for broken in (b"", b"{", b"[]", json.dumps({"results": {"bindings": "x"}}).encode(), None, 5):
        assert cables_open.meta_from_answer(broken) == []


def test_owner_lists_keep_companies_drop_persons_and_stay_short():
    assert cables_open.owner_text("Island Telco, Jane Roe, Coast Cable Inc") == "Island Telco, Coast Cable Inc"
    assert cables_open.owner_text("<Null>") is None and cables_open.owner_text("") is None
    long = ", ".join("Carrier Number %d Ltd" % i for i in range(12))
    out = cables_open.owner_text(long)
    assert len(out) <= 80 and out.endswith("…")


def test_an_ambiguous_name_matches_no_fact():
    meta = cables_open.meta_from_answer(sparql(binding("Q1", "Twin"), binding("Q2", "Twin Cable"), binding("Q3", "Solo")))
    idx = cables_open.meta_index(meta)
    assert cables_open.lookup(idx, "Twin") is None and cables_open.lookup(idx, "SOLO system")["wd"] == "Q3"


# ---------------------------------------------------------------------------------------------------------- national corridors
def test_geopackage_corridor_becomes_its_centre_line():
    pts = line(-30.0, 20.0, -28.0, 21.0, n=40)
    rings = cables_open.gpkg_geometry(gpkg_blob([corridor(pts)]))
    assert len(rings) == 1
    c = cables_open.centre_line(rings[0])
    assert abs(cables._length_km(c) - cables._length_km(pts)) < 3
    ends = sorted([c[0], c[-1]])
    assert abs(ends[0][0] + 30.0) < 0.01 and abs(ends[1][0] + 28.0) < 0.01
    for broken in (b"", b"XX" + b"\0" * 20, gpkg_blob([])[:12], None):
        assert cables_open.gpkg_geometry(broken) == []


def test_power_research_military_retired_and_out_of_service_cables_are_left_out():
    keep = cables_open.noaa_keep
    assert keep("Coral Link", "Island Telco", "In Service") and keep("Coral Link", None, None)
    for name, owner, status in (("Coral Power Cable", None, None), ("Coral Link", "Coast Electric Co", None), ("Utility Cables", None, None),
                                ("Possible telegraph cables", None, None), ("Ocean Observatory", None, None), ("Coral Link", None, "Abandoned"),
                                ("Coral Link", "Department of Defense", None), ("TAT-9", None, None), ("Bay XLPE", None, None),
                                ("Coral Link", None, "Out of Service"), (None, None, None)):
        assert not keep(name, owner, status), (name, owner, status)


def test_a_cable_that_leaves_national_waters_is_marked_part_and_only_ends_on_land_are_landings():
    rows = [("Coral Link", "Island Telco", "In Service", gpkg_blob([corridor(line(-30, 20, -25, 20))])),
            ("Reef Link", "Island Telco", None, gpkg_blob([corridor(line(-30, 22, -27, 22))])),
            ("Far Link", None, None, gpkg_blob([corridor(line(-20, 30, -19, 30))])),
            ("Coral Power Cable", None, None, gpkg_blob([corridor(line(-30, 24, -25, 24))]))]
    ways = cables_open.noaa_ways(rows)
    assert {w["n"] for w in ways} == {"Coral Link", "Reef Link", "Far Link"}

    def near(lon, lat):                                     # land: the coast at 30 W and the islands at 27 W
        return abs(lon + 30) < 0.2 or abs(lon + 27) < 0.2
    out = {e["n"]: e for e in cables_open.noaa_entries(ways, near)}
    assert set(out) == {"Coral Link", "Reef Link"}                   # all ends at sea: left out
    assert out["Coral Link"]["part"] == 1 and out["Coral Link"]["ends"][0] == out["Coral Link"]["ends"][1]
    assert "part" not in out["Reef Link"] and out["Reef Link"]["ends"][0] != out["Reef Link"]["ends"][1]
    assert all(e["src"] == "noaa" and re.match(r"^n\d+$", e["id"]) for e in out.values())
    g = cableroute.CableGraph(out["Coral Link"])
    assert len(g.landings) == 1 and abs(g.landings[0][1] + 30) < 0.05     # the end out at sea is no landing


# ---------------------------------------------------------------------------------------------------------- build and merge
def facts():
    return cables_open.meta_from_answer(sparql(
        binding("Q21", "Atlas Link", aliases="ATLAS-1", owners="Atlas Telco", service="2004-01-01T00:00:00Z", length="5000000", cap="40", unit="Q1084321"),
        binding("Q22", "Coral Link", owners="Island Telco", service="2018-01-01T00:00:00Z"),
        binding("Q23", "Old Link", ended="2005-01-01T00:00:00Z"),
        binding("Q24", "Reef Link", aliases="Reef Express")))


def test_build_drops_lines_the_community_map_has_by_name_or_by_the_same_item():
    base = base_set(("ATLAS-1", "Map Operator"), ("Reef Express", None))
    noaa = [{"id": "n0", "n": "Atlas Link", "op": None, "d": "M1 1l1 1", "km": 100, "ends": [[0, 0], [1, 1]], "src": "noaa"},
            {"id": "n1", "n": "Reef Link", "op": None, "d": "M1 1l1 1", "km": 100, "ends": [[0, 0], [1, 1]], "src": "noaa"},
            {"id": "n2", "n": "Coral Link", "op": None, "d": "M1 1l1 1", "km": 100, "ends": [[0, 0], [1, 1]], "src": "noaa"},
            {"id": "n3", "n": "Old Link", "op": None, "d": "M1 1l1 1", "km": 100, "ends": [[0, 0], [1, 1]], "src": "noaa"}]
    d = cables_open.build(base, noaa, facts(), "2026-11-22")
    assert [e["n"] for e in d["sea"]] == ["Coral Link"] and d["counts"]["dup"] == 2
    assert cables_open.problems(d, 1, 1) == []


def test_merge_fills_gaps_never_overwrites_drops_retired_cables_and_keeps_the_source():
    base = base_set(("ATLAS-1", "Map Operator"), ("Old Link", None), ("Nameless", None))
    extra = cables_open.build(base_set(), [{"id": "n0", "n": "Coral Link", "op": "Coral Operator", "d": "M1 1l1 1", "km": 100,
                                            "ends": [[0, 0], [1, 1]], "src": "noaa", "part": 1}], facts(), "2026-11-22")
    m = cables_open.merge(base, extra, 2026)
    by = {e["n"]: e for e in m["sea"]}
    assert set(by) == {"ATLAS-1", "Nameless", "Coral Link"}        # the retired cable left the lines
    atlas = by["ATLAS-1"]
    assert atlas["src"] == "osm" and atlas["op"] == "Map Operator" and atlas["own"] == "Atlas Telco"
    assert atlas["wd"] == "Q21" and atlas["yr"] == 2004 and atlas["cap"] == 40.0 and atlas["wkm"] == 5000
    assert by["Coral Link"]["src"] == "noaa" and by["Coral Link"]["op"] == "Coral Operator" and by["Coral Link"]["wd"] == "Q22"
    assert "wd" not in by["Nameless"] and by["Nameless"]["src"] == "osm"
    c = m["counts"]
    assert c["sea"] == 3 and c["osm_sea"] == 2 and c["open_sea"] == 1 and c["facts"] == 2 and c["retired"] == 1
    assert m["license"] == cables.LICENSE and m["attribution"] == cables.ATTRIBUTION and {s["id"] for s in m["sources"]} == {"noaa", "wikidata"}
    assert cables.problems(m, 1) == []
    assert cables_open.merge(base, None) is base and cables_open.merge(None, extra) is None
    again = cables_open.merge(base_set(("Coral Link", None)), extra, 2026)       # the community map has it now: drawn once
    assert [e["src"] for e in again["sea"]] == ["osm"] and again["counts"]["dup"] == 1


def test_problems_catch_a_broken_open_file():
    good = cables_open.build(base_set(), [], facts(), "2026-11-22")
    assert cables_open.problems(good, 1, 0) == []
    assert cables_open.problems({"format": "x"}) == ["format"]
    bad = json.loads(json.dumps(good))
    bad["sources"] = []
    bad["meta"][0]["wd"] = "X1"
    bad["sea"] = [{"id": "n0", "n": "A", "op": None, "d": "nope", "km": 1, "ends": [[0, 0], [1, 1]], "src": "elsewhere"}]
    p = cables_open.problems(bad, 1, 0)
    assert "sources" in p and "meta[0]" in p and "sea[0] src" in p and "sea[0] geometry" in p
    assert any(x.startswith("too few facts") for x in cables_open.problems(good, 100, 0))


# ---------------------------------------------------------------------------------------------------------- build script
def raw_folder(tmp_path, n_lines=25, n_facts=120):
    raw = tmp_path / "raw"
    raw.mkdir()
    gp = tmp_path / "c.gpkg"
    rows = [("Coral Link %d" % i, "Island Telco", "In Service", [corridor(line(-157.9, 21.3 + i * 0.001, -155.1, 19.7 + i * 0.001))]) for i in range(n_lines)]
    rows.append(("Ocean Link", "Island Telco", None, [corridor(line(-157.9, 21.3, -150.0, 25.0))]))   # leaves national waters
    gpkg_file(gp, rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(gp, "Cables.gpkg")
    (raw / "noaa.zip").write_bytes(buf.getvalue())
    b = [binding("Q%d" % (100 + i), "Fact Link %d" % i, owners="Owner Telco %d" % i, service="2010-01-01T00:00:00Z",
                 length="6000000", cc="XA|XB") for i in range(n_facts)]
    (raw / "wikidata.json").write_bytes(sparql(*b))
    return raw


def n_lines_plus_one(n):
    return n + 1


def test_build_script_writes_the_open_file_atomically_with_a_checksum(tmp_path):
    raw = raw_folder(tmp_path)
    out = tmp_path / "cables-open.json.gz"
    rc = build_script.main(["--step", "open", "--from-raw", str(raw), "--out", str(cables.DATA_FILE), "--out-open", str(out), "--date", "2026-11-22"])
    assert rc == 0 and out.is_file()
    blob = out.read_bytes()
    assert (tmp_path / "cables-open.sha256").read_text().split()[0] == hashlib.sha256(blob).hexdigest()
    d = cables_open.decode(blob)
    assert len(d["sea"]) == n_lines_plus_one(25) and len(d["meta"]) == 120
    assert [e["n"] for e in d["sea"] if e.get("part")] == ["Ocean Link"]


def test_build_script_refuses_thin_answers_and_writes_nothing(tmp_path):
    raw = raw_folder(tmp_path, n_lines=3, n_facts=10)
    out = tmp_path / "cables-open.json.gz"
    rc = build_script.main(["--step", "open", "--from-raw", str(raw), "--out", str(cables.DATA_FILE), "--out-open", str(out), "--date", "2026-11-22"])
    assert rc == 1 and not out.exists() and not (tmp_path / "cables-open.sha256").exists()


def test_a_zip_without_exactly_one_geopackage_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(ValueError):
        build_script.noaa_from_zip(buf.getvalue())


# ---------------------------------------------------------------------------------------------------------- run time
def test_cable_data_serves_the_merged_set_with_the_open_sources(tmp_path):
    cd = cables.CableData(tmp_path)
    i = cd.info()
    assert i["available"] and i["open_sea"] > 0 and i["facts"] > 0 and i["sea"] == i["osm_sea"] + i["open_sea"]
    assert {s["id"] for s in i["sources"]} == {"noaa", "wikidata"} and all(s["license"] == "CC0 1.0" for s in i["sources"])
    d = cd.data()
    assert cables.problems(d) == []
    assert {e["src"] for e in d["sea"]} == {"osm", "noaa"}
    assert len({e["id"] for e in d["sea"] + d["land"]}) == len(d["sea"]) + len(d["land"])
    blob, etag = cd.blob()
    assert cables.decode(blob)["counts"]["open_sea"] == i["open_sea"] and etag


def test_a_broken_open_file_leaves_the_community_cables_alone(tmp_path):
    bad = tmp_path / "open.json.gz"
    bad.write_bytes(b"broken")
    cd = cables.CableData(None, open_file=bad)
    i = cd.info()
    assert i["available"] and i["open_sea"] == 0 and i["sources"] == [] and not cd.data().get("sources")
    other = cables.CableData(None, bundled=cables.DATA_FILE, open_file=tmp_path / "missing.json.gz")
    assert other.info()["open_sea"] == 0
