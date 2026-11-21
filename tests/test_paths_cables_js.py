"""Real cables view (DW3) in the browser code: Real cables is the default, the choice is stored and follows the address,
a deep link wins once, and the line helpers the dots run on (length, position, arcs, start point, cut at a country)."""
from _board_node import run_node


def test_default_view_switch_store_and_address():
    r = run_node("paths_cables_test.js")
    assert r["first"] == "cables" and r["on"] is True
    assert r["afterRain"]["view"] == "rain" and r["afterRain"]["stored"] == "rain" and r["afterRain"]["hash"] == "#/paths?view=rain"
    assert r["afterRain"]["event"] == ["lanloom-paths-view", {"view": "rain"}]
    assert r["deep"] == {"view": "cables", "stored": "rain"}                   # a deep link does not change the stored choice
    assert r["chosen"]["stored"] == "cables" and r["chosen"]["hash"] == "#/paths?view=cables"
    assert r["ignored"] == "cables"
    assert "paths-view-cables" in r["cmds"] and "paths-view-rain" in r["cmds"]


def test_line_helpers():
    r = run_node("paths_cables_test.js")
    assert r["line"]["len"] == 11 and r["line"]["l"] == [0, 5, 11]
    assert r["line"]["start"] == [0, 0] and r["line"]["end"] == [3, 10] and r["line"]["over"] == [3, 10]
    assert abs(r["line"]["mid"][0] - 3) < 1e-9 and abs(r["line"]["mid"][1] - 4.5) < 1e-9
    assert r["jump"] == 4
    assert r["pathLen"] > 60
    assert r["gone"] == [] and r["legendOff"] == [], "Real cables knows no arc, no cut and no dashed path part"
    assert r["d"] == "M1.00 2.00L3.33 4.00M998.00 4.00M2.00 4.00"          # jumps over 400 units (date line) are not drawn


def test_tip_without_operator_has_no_null_line():
    """lanloom#12: a cable without operator showed the text "null" as the third tip line."""
    r = run_node("paths_cables_tip_test.js")
    for k in ("none", "missing", "empty", "literal"):
        assert len(r[k]) == 3 and not any("null" in x.lower() or "operator" in x for x in r[k]), (k, r[k])
    assert len(r["withOp"]) == 4 and "Consortium" in r["withOp"][2]


def test_no_null_children_in_direct_dom_calls():
    """replaceChildren/append turn a null argument into the text "null": no `: null` child may go in unfiltered."""
    import pathlib
    import re
    bad = []
    for p in pathlib.Path(__file__).resolve().parent.parent.joinpath("lanloom", "web", "js").glob("*.js"):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"\.replaceChildren\(\s*(?:[\w.!()]+|[\w.]+\(\)) \? [^\n]*: null,", text):   # a conditional first argument
            if ".filter(Boolean)" not in text[m.start():m.start() + 600]:
                bad.append("%s:%d" % (p.name, text.count("\n", 0, m.start()) + 1))
    assert not bad, bad
