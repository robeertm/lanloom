"""Tests for scripts/build_route_places.py and the shipped lanloom/data/route-places.json."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build_route_places", ROOT / "scripts" / "build_route_places.py")
brp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brp)

HEADER = "id,ident,type,name,latitude_deg,longitude_deg,iso_country,scheduled_service,iata_code\n"
ROWS = [
    ("1", "EDDF", "large_airport", "Test Field A", 50.0333, 8.5706, "DE", "yes", "FRA"),
    ("2", "EGLL", "large_airport", "Test Field B", 51.4706, -0.4619, "GB", "yes", "LHR"),
    ("3", "XXXX", "heliport", "Test Heliport", 10.0, 10.0, "DE", "no", "AMS"),
    ("4", "XXXY", "small_airport", "Small No Service", 1.0, 1.0, "NL", "no", "AMS"),
    ("5", "EHAM", "large_airport", "Test Field C", 52.3086, 4.7639, "NL", "yes", "AMS"),
]
SMALL = dict(min_cities=3, min_keys=5, samples={"fra": (50.03, 8.56, 0.5)})


def _csv(tmp_path, rows=ROWS):
    p = tmp_path / "airports.csv"
    p.write_text(HEADER + "".join(",".join(str(c) for c in r) + "\n" for r in rows), encoding="utf-8")
    return p


def test_read_prefers_larger_and_scheduled(tmp_path):
    ap = brp.read(_csv(tmp_path))
    assert ap["AMS"] == (52.3086, 4.7639)      # heliport ignored, large airport wins
    assert ap["FRA"] == (50.0333, 8.5706)


def test_build_reports_missing_and_keys(tmp_path):
    places, missing = brp.build(brp.read(_csv(tmp_path)))
    assert missing and any("(JFK)" in m for m in missing)
    assert places["fra"]["cc"] == "DE" and places["defra"] == places["frankfurt"]
    assert places["ukLHR".lower()]["name"] == "London" and places["gblon"]["name"] == "London"


def test_exclusion_whole_word_any_case(tmp_path):
    ap = brp.read(_csv(tmp_path))
    words = tmp_path / "x.txt"
    words.write_text("# comment\nfrankfurt\n", encoding="utf-8")
    places, _ = brp.build(ap, brp._excluder(words))
    assert "fra" not in places and "frankfurt" not in places
    assert "ams" in places
    words.write_text("FRANK\n", encoding="utf-8")                  # not a whole word
    assert "fra" in brp.build(ap, brp._excluder(words))[0]
    words.write_text("LONDON\n", encoding="utf-8")                 # any case, also removes the extra keys
    places, _ = brp.build(ap, brp._excluder(words))
    assert "lhr" not in places and "gblon" not in places


def test_check_passes_small_and_fails_bad(tmp_path):
    places, _ = brp.build(brp.read(_csv(tmp_path)))
    brp.check(places, **SMALL)
    bad = {k: dict(v) for k, v in places.items()}
    bad["fra"]["lat"] = 95.0
    with pytest.raises(ValueError):
        brp.check(bad, **SMALL)
    bad = {k: dict(v) for k, v in places.items()}
    bad["fra"]["lon"] = 200
    with pytest.raises(ValueError):
        brp.check(bad, **SMALL)
    bad = {k: dict(v) for k, v in places.items()}
    bad["fra"]["cc"] = "de"
    with pytest.raises(ValueError):
        brp.check(bad, **SMALL)
    with pytest.raises(ValueError):
        brp.check(places, min_cities=3, min_keys=5, samples={"fra": (0.0, 0.0, 0.5)})


def test_main_fails_without_partial_file(tmp_path):
    out = tmp_path / "out.json"
    assert brp.main([str(_csv(tmp_path)), "--out", str(out)]) == 1   # cities missing
    assert not out.exists() and not list(tmp_path.glob("*.tmp"))


def test_write_is_atomic_and_deterministic(tmp_path, monkeypatch):
    places, _ = brp.build(brp.read(_csv(tmp_path)))
    out = tmp_path / "o.json"
    brp.write(places, out)
    first = out.read_bytes()
    brp.write(dict(reversed(list(places.items()))), out)
    assert out.read_bytes() == first
    monkeypatch.setattr(brp.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    out2 = tmp_path / "o2.json"
    with pytest.raises(OSError):
        brp.write(places, out2)
    assert not out2.exists() and not list(tmp_path.glob("*.tmp"))


@pytest.fixture(scope="module")
def shipped():
    return json.loads((ROOT / "lanloom" / "data" / "route-places.json").read_text(encoding="utf-8"))


def test_shipped_file(shipped):
    assert shipped["head"]["format"] == "route-places/1"
    places = shipped["places"]
    assert len({(p["name"], p["cc"]) for p in places.values()}) >= 250
    assert len(places) >= 600
    for key, p in places.items():
        assert re.match(r"^[a-z]{3,20}$", key)
        assert re.match(r"^[A-Z]{2}$", p["cc"])
        assert -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180
    for key, cc in {"fra": "DE", "ams": "NL", "lhr": "GB", "jfk": "US", "sin": "SG"}.items():
        assert places[key]["cc"] == cc
    brp.check(places)
