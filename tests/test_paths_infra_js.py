"""Infrastructure strands of the Real cables view in the browser code: only lines (no arc, nothing dashed), the shares add up to the
traffic, bundling, satellite hop with its symbol, legend with the drawn lines only, the country sheet's way over the inland net, one
fetch per stand; plus static checks that the stylesheet and the map code of that view contain no dash and no arc."""
import re
from pathlib import Path

from _board_node import run_node

WEB = Path(__file__).resolve().parent.parent / "lanloom" / "web"


def test_shares_bundles_satellite_and_legend():
    r = run_node("paths_infra_test.js")
    assert r["shareSum"] == r["targetSum"] == 140 and r["streamSum"] == 140, "the sum of the strands is the sum of the map"
    assert r["shareCount"] == 4                                      # XA over two measured ways, XB, XC
    assert r["noArc"] and r["noDash"]
    assert r["bundled"] == 1 and r["measuredFirst"], "equal strands share one element; measured stays measured"
    assert r["sats"] == 2 and "sat" in r["arts"] and "measured" in r["arts"]
    assert r["satShare"] == [["sat", "leo"]], "a satellite operator runs over the earth station to the destination"
    assert r["conflictEnd"] == [[[10, 0], "Alpha"]], "anycast: the way ends where it was measured"
    assert "infra.origin.route" in r["endText"] and "infra.measured_at" in r["endText"]
    assert r["legendNoSea"] and len(r["legend"]) == len(r["arts"])
    assert {"cable", "land", "radio", "sat"} <= set(r["kinds"]) and "sea" not in r["kinds"]
    assert r["fetched"] == ["/api/paths/infra?stand=s1"] and r["event"] == 1 and r["data"] == "s1", "one fetch per stand"


def test_country_sheet_cuts_at_the_first_hub_and_goes_inland():
    r = run_node("paths_infra_test.js")
    s = r["sheet"]
    assert len(s) == 1 and s[0]["bytes"] == 100
    assert s[0]["ids"] == ["1", "2", "5"] or s[0]["ids"] == ["1", "2"] + ["5"], "cut at the first hub of the country (Beta), then on to the hub nearest the city"
    assert s[0]["tail"] == [[27, 10]] and s[0]["origin"] == "route" and s[0]["measured"] == 1
    assert r["sheetStreams"] == 1 and r["sheetNoArc"]
    assert r["sheetSat"] == [["sat", "leo", [10, 0]]]


def test_stylesheet_and_map_code_have_no_dash_and_no_arc():
    for css in ("paths_infra.css", "paths_cables.css"):
        assert "dasharray" not in (WEB / "css" / css).read_text(encoding="utf-8"), css
    infra = (WEB / "js" / "paths_infra.js").read_text(encoding="utf-8")
    assert "dasharray" not in infra and "pw-arc" not in infra
    paths = (WEB / "js" / "paths.js").read_text(encoding="utf-8")
    block = paths[paths.index("const IN = cables ? INF()"):paths.index("if (hp && !cables) {")]
    assert "pw-arc" not in block and "arcGeom" not in block and "arcD" not in block, "Real cables never draws an arc"
    assert "paths_infra.js" in (WEB / "index.html").read_text(encoding="utf-8") and "paths_infra.css" in (WEB / "index.html").read_text(encoding="utf-8")
    country = (WEB / "js" / "paths_country.js").read_text(encoding="utf-8")
    assert "air" not in re.findall(r"kind: '(\w+)'", country) and "pathGroup" not in country
