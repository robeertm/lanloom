"""Board layout: no overlaps, tiles inside zones, zone rows aligned at both margins, two columns on phones."""
from _board_node import run_node


def test_layout_geometry():
    out = run_node("layout_test.js")
    assert len(out) == 24
    for case in out:
        assert case["issues"] == [], case
        assert case["compact"] == (case["w"] <= 700), case
