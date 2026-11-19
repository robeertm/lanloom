"""Checks for the generated offline world base map (no network)."""
import re
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "lanloom" / "web" / "js" / "worldmap.js"


def test_worldmap_generated_file():
    assert FILE.is_file()
    raw = FILE.read_bytes()
    assert len(raw) < 80 * 1024
    text = raw.decode("utf-8")
    assert "GENERATED" in text
    assert '"DE":{"d":"M' in text
    assert '"US":' in text
    assert "Lanloom.worldmap" in text
    body = text.split("*/", 1)[1]
    assert not re.search(r"[^\x00-\x7f]", body)


# ---------------------------------------------------------------------- overseas parts and territories
def _load_map():
    import json
    src = FILE.read_text(encoding="utf-8")
    dec = json.JSONDecoder()
    countries, _ = dec.raw_decode(src[src.index("countries: ") + len("countries: "):])
    points, _ = dec.raw_decode(src[src.index("points: ") + len("points: "):])
    return countries, points


def _builder():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_worldmap", FILE.parent.parent.parent.parent / "scripts" / "build_worldmap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_country_lights_only_its_main_area():
    """France must not light up South America: French Guiana is its own shape (its own ISO code)."""
    b = _builder()
    countries, _ = _load_map()
    fr = b._parse_rings(countries["FR"]["d"])
    lon_lat = [b.unproject(sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) for _, pts, _ in fr]
    assert all(lon > -20 and lat > 35 for lon, lat in lon_lat), lon_lat          # only Europe (mainland, Corsica)
    gf = countries["GF"]
    lon, lat = b.unproject(gf["x"], gf["y"])
    assert -55 < lon < -51 and 1.5 < lat < 6


def test_no_shape_keeps_a_far_away_part():
    b = _builder()
    countries, _ = _load_map()
    for cc, c in countries.items():
        rings = b._parse_rings(c["d"])
        main = max(rings, key=lambda r: r[2])
        kept = [main]
        rest = [r for r in rings if r is not main]
        while True:
            near = [r for r in rest if any(b._gap(r[1], k[1]) <= b.OVERSEAS_DIST for k in kept)]
            if not near:
                break
            kept += near
            rest = [r for r in rest if r not in near]
        assert not rest, cc


def test_refine_moves_far_parts_and_keeps_neighbours_across_the_date_line():
    b = _builder()
    ring = lambda lon, lat, s=2.0: b._ring_path([b.project(lon, lat), b.project(lon + s, lat), b.project(lon + s, lat + s),
                                                 b.project(lon, lat + s)])
    countries = {
        "AA": {"d": ring(0, 45, 8) + ring(-60, 10), "x": 1.0, "y": 1.0},          # far part, no own code -> o
        "BB": {"d": ring(170, 64, 8) + ring(-178, 64), "x": 1.0, "y": 1.0},       # across the date line -> stays
        "FR": {"d": ring(0, 44, 6) + ring(-54, 3, 1.5), "x": 1.0, "y": 1.0},      # territory with its own code
    }
    points = {}
    b.refine(countries, points)
    assert countries["AA"]["d"] == ring(0, 45, 8) and countries["AA"]["o"] == ring(-60, 10)
    assert "o" not in countries["BB"] and countries["BB"]["d"] == ring(170, 64, 8) + ring(-178, 64)
    assert countries["FR"]["d"] == ring(0, 44, 6) and "o" not in countries["FR"] and countries["GF"]["d"] == ring(-54, 3, 1.5)
    assert "RE" in points and "GF" not in points


def test_every_country_of_the_geo_data_can_be_drawn():
    """Each code the bundled geo data can return has a shape or a point (otherwise its traffic would be invisible)."""
    import gzip
    import json
    import struct
    from lanloom import geo
    raw = gzip.decompress(geo.DATA_FILE.read_bytes())
    off = len(geo.MAGIC)
    (hl,) = struct.unpack(">I", raw[off:off + 4])
    codes = set(json.loads(raw[off + 4:off + 4 + hl])["codes"]) - {geo.RESERVED}
    countries, points = _load_map()
    assert codes <= set(countries) | set(points), sorted(codes - set(countries) - set(points))


def test_the_view_draws_overseas_parts_unlit():
    js = (FILE.parent / "paths.js").read_text(encoding="utf-8")
    assert "c.o" in js and "pw-overseas" in js


# ---------------------------------------------------------------------- fine map and orientation places
JS_DIR = FILE.parent
FINE_FILE = JS_DIR / "worldmap_fine.js"
PLACES_FILE = JS_DIR / "worldplaces.js"
FINE_BUDGET = 700 * 1024
PLACES_BUDGET = 100 * 1024


def _load_fine():
    import json
    src = FINE_FILE.read_text(encoding="utf-8")
    return json.JSONDecoder().raw_decode(src[src.index("countries: ") + len("countries: "):])[0]


def _load_places():
    import json
    src = PLACES_FILE.read_text(encoding="utf-8")
    start = src.index("worldplaces = ") + len("worldplaces = ")
    return json.JSONDecoder().raw_decode(src[start:])[0]


def _geo_codes():
    from lanloom import geo
    return set(geo.GeoDB.from_file(geo.DATA_FILE).codes) - {geo.RESERVED}


def _numbers_of(path):
    """Absolute coordinates of every point of a path in the generator's format (M x y l dx dy ... z)."""
    b = _builder()
    out = []
    for _, pts, _ in b._parse_rings(path):
        out += pts
    return out


def test_fine_and_places_files_are_generated_and_in_budget():
    for f, budget, marker in ((FINE_FILE, FINE_BUDGET, "Lanloom.worldmapFine"), (PLACES_FILE, PLACES_BUDGET, "Lanloom.worldplaces")):
        assert f.is_file(), f
        raw = f.read_bytes()
        assert len(raw) < budget, (f.name, len(raw))
        text = raw.decode("utf-8")
        head = text.split("*/", 1)[0]
        assert "GENERATED" in head and "Natural Earth (public domain)" in head and "build_worldmap.py" in head
        assert "ne_10m_populated_places_simple.geojson" in head
        assert marker in text
        assert not re.search(r"[^\x00-\x7f]", text)


def test_every_country_of_the_geo_data_is_drawable():
    """Each code in the bundled geo data has a shape or a point in the base map, or a shape in the fine map."""
    countries, points = _load_map()
    fine = _load_fine()
    codes = _geo_codes()
    assert codes, "no codes"
    missing = codes - set(countries) - set(points) - set(fine)
    assert not missing, sorted(missing)


def test_fine_map_shapes_are_valid_and_inside_the_view_box():
    b = _builder()
    fine = _load_fine()
    countries, points = _load_map()
    assert len(fine) > 200
    assert fine.get("DE") and fine.get("US")
    for cc, c in fine.items():
        assert len(cc) == 2 and cc.isupper(), cc
        assert c.get("d"), cc
        if cc == "AQ":      # Antarctica reaches far south of the view box (-58 degrees) and is cropped by it, as in the base map
            continue
        for key in ("d", "o"):
            if key not in c:
                continue
            pts = _numbers_of(c[key])
            assert pts, (cc, key)
            for x, y in pts:
                assert -5 <= x <= b.WIDTH + 5 and -5 <= y <= b.HEIGHT + 5, (cc, key, x, y)
    # small states that are only a point in the base map and have a 1:50m shape get it here
    assert "AD" in points and "AD" in fine


def test_fine_map_keeps_only_the_main_area_in_d():
    b = _builder()
    for cc, c in _load_fine().items():
        rings = b._parse_rings(c["d"])
        main = max(rings, key=lambda r: r[2])
        kept = [main]
        rest = [r for r in rings if r is not main]
        while True:
            near = [r for r in rest if any(b._gap(r[1], k[1], b.SAMPLE_POINTS) <= b.OVERSEAS_DIST for k in kept)]
            if not near:
                break
            kept += near
            rest = [r for r in rest if r not in near]
        assert not rest, cc


def test_places_are_ranked_inside_the_map_and_use_known_country_codes():
    b = _builder()
    places = _load_places()
    codes = _geo_codes()
    assert 1000 < len(places) < 6000
    assert [p[4] for p in places] == sorted(p[4] for p in places)
    assert {p[4] for p in places} == {0, 1, 2, 3}
    for p in places:
        assert len(p) == 5, p
        x, y, name, cc, rank = p
        assert isinstance(name, str) and name.strip() and len(name) <= b.PLACE_NAME_MAX * 2, p
        assert rank in (0, 1, 2, 3), p
        assert 0 <= x <= b.WIDTH and 0 <= y <= b.HEIGHT, p
        assert cc in codes, p
    # sorted by rank, then name
    keys = [(p[4], p[2]) for p in places]
    assert keys == sorted(keys)
    names = {p[2] for p in places}
    assert {"Berlin", "Paris", "Tokyo", "Washington, D.C."} & names


# ---------------------------------------------------------------------- the build routines (synthetic input, no network)
def _square(lon, lat, s):
    return [[[lon, lat], [lon + s, lat], [lon + s, lat + s], [lon, lat + s], [lon, lat]]]


def _feature(iso, polygons):
    return {"type": "Feature", "properties": {"ISO_A2": iso, "ISO_A2_EH": iso},
            "geometry": {"type": "MultiPolygon", "coordinates": polygons}}


def _mini_countries():
    return [
        _feature("AA", [_square(0, 45, 8), _square(-60, 10, 3)]),             # far part, no own code -> `o`
        _feature("BB", [_square(10, -30, 10)]),
        _feature("CC", [_square(100, 20, 0.3)]),
    ]


def _mini_places():
    def place(name, lon, lat, sr, cls="Populated place", cc="AA", cap=0):
        return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"name": name, "nameascii": name, "scalerank": sr, "featurecla": cls, "adm0cap": cap,
                               "iso_a2": cc, "adm0_a3": "AAA", "longitude": lon, "latitude": lat}}
    return [place("Zeta", 5, 48, 5), place("Alpha", 4, 47, 5), place("Capital", 3, 46, 9, "Admin-0 capital", cap=1),
            place("Big", 12, -25, 1, cc="BB"), place("Medium", 11, -24, 3, cc="BB"), place("Tiny", 13, -26, 9, cc="BB"),
            place("Odd", 14, -27, 2, cc="-99"), place("Kos", 21, 42, 4, cc="-99")]


def test_build_places_ranks_sorts_and_resolves_codes():
    b = _builder()
    feats = _mini_places()
    feats[-1]["properties"]["adm0_a3"] = "KOS"
    out = b.build_places(feats)
    by = {p[2]: p for p in out}
    assert "Tiny" not in by and "Odd" not in by                           # too small / no country code
    assert by["Capital"][4] == 0 and by["Big"][4] == 0 and by["Medium"][4] == 1 and by["Alpha"][4] == 2
    assert by["Kos"][3] == "XK" and by["Kos"][4] == 2
    assert [(p[4], p[2]) for p in out] == sorted((p[4], p[2]) for p in out)
    x, y = b.project(3, 46)
    assert by["Capital"][:2] == [round(x, 2), round(y, 2)]


def test_round_trip_build_routines_are_deterministic_and_match_the_committed_format():
    import json
    b = _builder()
    fine = b.build_fine(_mini_countries())
    assert fine == b.build_fine(_mini_countries())
    assert set(fine) == {"AA", "BB", "CC"} and all(c["d"].startswith("M") for c in fine.values())
    assert fine["AA"]["d"] and fine["AA"]["o"].startswith("M")             # far part moved out of d
    assert "x" not in fine["BB"]                                           # no label points in the fine map
    places = b.build_places(_mini_places())
    # the rendered text carries exactly the data, in the format the committed files use
    for text, key, data in ((b.render_fine(fine), "countries: ", fine), (b.render_places(places), "worldplaces = ", places)):
        assert text == (b.render_fine if key == "countries: " else b.render_places)(json.loads(json.dumps(data)))
        start = text.index(key) + len(key)
        assert json.JSONDecoder().raw_decode(text[start:])[0] == data
        head = text.split("*/", 1)[0]
        assert "GENERATED" in head and "Natural Earth (public domain)" in head
    # same layout as the committed files: header, then the IIFE that registers on window.Lanloom
    for committed, text in ((FINE_FILE, b.render_fine(fine)), (PLACES_FILE, b.render_places(places))):
        real = committed.read_text(encoding="utf-8")
        assert real.split("*/", 1)[0] == text.split("*/", 1)[0]
        assert real.split("*/", 1)[1].split("\n", 4)[:4] == text.split("*/", 1)[1].split("\n", 4)[:4]


def test_build_main_writes_all_three_files_from_a_mini_input(tmp_path, monkeypatch):
    import json
    b = _builder()
    for name, feats in (("c110.geojson", _mini_countries()), ("c50.geojson", _mini_countries()), ("pl.geojson", _mini_places())):
        (tmp_path / name).write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    out = {n: tmp_path / (n + ".js") for n in ("OUT", "OUT_FINE", "OUT_PLACES")}
    for n, p in out.items():
        monkeypatch.setattr(b, n, p)
    args = [str(tmp_path / n) for n in ("c110.geojson", "c50.geojson", "pl.geojson")]
    b.main(*args)
    first = {n: p.read_bytes() for n, p in out.items()}
    assert all(first.values())
    b.main(*args)
    assert {n: p.read_bytes() for n, p in out.items()} == first            # deterministic
    # the older two-argument form writes only the base map
    for n in ("OUT_FINE", "OUT_PLACES"):
        out[n].unlink()
    b.main(*args[:2])
    assert out["OUT"].is_file() and not out["OUT_FINE"].exists() and not out["OUT_PLACES"].exists()
