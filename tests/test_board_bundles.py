"""Bundled traces: lanes to the same target and port merge into one trunk, everything stays octilinear and off foreign
tiles, separate keys never merge, the routed input is left untouched."""
from _board_node import run_node


def test_bundle_geometry():
    g = run_node("bundles_test.js")["geometry"]
    a = g["a"]
    assert a["pts"][0] == [100, 0] and a["pts"][1][0] == 100, a
    assert abs(a["shared"] - 50) < 0.01 and a["junction"] == [100, 50], a
    b = g["b"]
    assert b["pts"][:2] == [[100, 0], [100, 50]] and b["pts"][2][1] == 50 and b["shared"] > 120, b
    assert g["blocked"] is None and g["far"] is None
    assert g["ortho"] == [[0, 0], [0, 50], [60, 50]] and g["chamfer"] == [[0, 0], [0, 48], [2, 50], [60, 50]]
    assert g["fromTarget"] == [[2, 2], [1, 1]]


def test_bundle_whole_board():
    out = run_node("bundles_test.js")
    b = out["board"]
    assert b["traces"] == b["routed"] == b["nets"], b
    assert b["members"] > 0 and b["trunks"] > 0 and b["maxMembers"] > 2, b
    assert b["inputUntouched"] and b["issues"] == [], b
    assert out["none"] == {"members": 0, "maxMembers": 1}
