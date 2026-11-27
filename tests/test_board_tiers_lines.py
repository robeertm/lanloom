"""Lines of the bus layout (board/tiers.js busRoutes + layout.js): rectangular, few bends, no crossings (lanloom#29)."""
from _board_node import run_node


def _out():
    if not hasattr(_out, "cache"):
        _out.cache = run_node("tiers_lines_test.js")
    return _out.cache


def test_guest_box_under_a_hypervisor_has_at_most_two_bends():
    # parent pad → one vertical line → branch → drop: no zigzag along the whole row, at every width
    for width, v in _out()["widths"].items():
        g = v["hv"]["guests"]
        assert g["n"] == 3 and g["onBus"], (width, g)
        assert g["maxBends"] <= 2, (width, g)
        assert g["slanted"] == 0 and g["tileHits"] == 0, (width, g)


def test_radio_box_of_a_repeater_has_no_diagonal_and_at_most_two_bends():
    # the radio box of the repeater lies under the repeater, so its line no longer crosses a neighbouring box
    for width, v in _out()["widths"].items():
        r = v["rep"]["repeater"]
        assert r["n"] == 6 and r["onBus"], (width, r)
        assert r["maxBends"] <= 2, (width, r)
        assert r["slanted"] == 0 and r["tileHits"] == 0, (width, r)
        # both boxes in one row (guests and repeater boxes side by side) still keep the repeater line short
        assert v["both"]["repeater"]["maxBends"] <= 2, (width, v["both"]["repeater"])


def test_no_diagonals_no_crossings_no_tile_hits_anywhere():
    for width, view in _out()["widths"].items():
        for name, v in view.items():
            assert v["slantedAnywhere"] == 0, (width, name)
            assert v["crossings"] == 0, (width, name)
            assert v["all"]["tileHits"] == 0, (width, name)
            assert v["allCovered"] and v["startsAtPad"] and v["endsOnTile"], (width, name, v)
            assert v["all"]["maxBends"] <= 4, (width, name, v["all"])      # a blocked way in costs one detour, never a zigzag


def test_plain_layout_is_not_touched():
    out = _out()
    assert out["plain_has_parent"] is False    # only the bus view carries parents (and so row order and block shifts)
    assert out["plain_rows"] == 3
