"""Resilience (#20): the word of a small square names the status the "Why?" panel shows, and the "Also affected" line drops
the zeros and gives every group its own plural (Node, pure helpers of lanloom/web/js/resilience.js)."""
import json
from pathlib import Path

from _board_node import run_node

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "lanloom" / "web"
LANGS = ("en", "de", "fr", "es", "nl", "it")


def test_square_word_and_parts_line():
    r = run_node("resilience_test.js")
    assert r["steps"] == ["good", "fair", "weak", "bad", "unknown", "na", "unknown"]
    # a fully weak axis is not "missing": mains only and a low availability are measured states (#20)
    assert r["mains"] == "rs.axis.power: rs.value.mains · rs.weak.bad"
    assert r["measured"] == "rs.axis.health: rs.value.measured · rs.weak.bad"
    assert r["spare"] == "rs.axis.spare: rs.value.none · rs.weak.bad"
    assert r["ups"] == "rs.axis.power: rs.value.ups · rs.weak.good"
    assert r["unknown"] == "rs.axis.power: rs.value.unknown"
    assert r["notRated"] == "rs.axis.backup: rs.value.not_rated"
    assert r["bare"] == "rs.axis.watch: rs.weak.weak"
    assert r["parts"] == "fail.part.hub_devices(1) · fail.part.entities(3)"
    assert r["allParts"] == "fail.part.devices(29) · fail.part.hub_devices(41) · fail.part.entities(457) · fail.part.connections(4)"
    assert r["noParts"] == "" and r["nullParts"] == ""
    assert r["order"] == ["devices", "hub_devices", "automations", "entities", "connections"]


def test_the_red_word_is_not_missing():
    """#20: the red square stood for "missing" even where the panel says "mains only" or "measured"."""
    en = json.loads((ROOT / "lanloom" / "i18n" / "en.json").read_text())
    assert "missing" not in en["rs.weak.bad"].lower()
    for lang in LANGS:
        keys = json.loads((ROOT / "lanloom" / "i18n" / (lang + ".json")).read_text())
        assert "rs.why.impact_sure" not in keys and "rs.why.impact_also" in keys and "rs.part.link.sleepy" in keys


def test_panel_wiring():
    src = (WEB / "js" / "resilience.js").read_text()
    assert "label = cellLabel(axis, a, t)" in src, "the square takes its word from the status, not from the rating alone"
    assert "partsLine(v, t)" in src and "r.key === 'impact_also'" in src, "the browser fills {parts} of rs.why.impact_also"
    assert "rs-pill" in src and "rs-axis__lead" in src and "rs-axis__detail" in src and "axisLink(a, d, box)" in src
    assert "rs-axis__why" not in src
    css = (WEB / "css" / "resilience.css").read_text()
    for cls in (".rs-pill--bad", ".rs-pill--unknown", ".rs-axis__lead", ".rs-axis__detail", ".rs-axis__link"):
        assert cls in css
