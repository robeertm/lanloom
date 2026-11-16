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
