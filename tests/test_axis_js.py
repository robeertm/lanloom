"""Axis block of the history charts and the label arrangement of the 3D view (Node)."""
from pathlib import Path

from _board_node import run_node

WEB = Path(__file__).resolve().parent.parent / "lanloom" / "web"


def test_axis_and_labels():
    r = run_node("axis_test.js")
    assert r["nice"] == [6, 1.2, 0.08, 150, 3, 10, 1000], "fine steps: 5.2 does not jump to 10"
    assert r["calm"]["clipped"] is False and r["calm"]["hi"] <= 300
    assert r["spiky"]["clipped"] is True and r["spiky"]["top"] == 2000 and r["spiky"]["hi"] <= 300, "a lone spike does not set the axis"
    assert (r["pct"]["lo"], r["pct"]["hi"]) == (0, 100)
    assert r["flat"]["hi"] > r["flat"]["lo"]
    assert len(r["small"]) == 2 and len(r["normal"]) == 4 and r["empty"]["hi"] == 1
    assert r["overlap"] == 0 and r["inside"], "measured labels never overlap and stay entirely in the picture"
    assert r["focusShown"] and r["lifted"] >= 1 and r["same"]


def test_wiring():
    assert "js/axis.js" in (WEB / "index.html").read_text()
    s = (WEB / "js" / "sensors.js").read_text()
    assert "LL.axis.scale" in s and "axis.clipped" in s
    infra = (WEB / "js" / "paths_infra.js").read_text()
    assert "svgEl('title')" not in infra and "data-tip" in infra, "one shared sign instead of a title per line"


def test_label_arrangement_lives_inside_the_factory():
    """mount() draws the labels with arrangeLabels: the function has to be defined inside the factory, not in the module wrapper
    around it (there the page threw "arrangeLabels is not defined" on every frame of the 3D view, only the Node test saw it)."""
    src = (WEB / "board" / "scene3d.js").read_text()
    factory = src[src.index("function (TreeArg) {"):]
    assert "function arrangeLabels(" in factory and "arrangeLabels(items" in factory
    assert "function arrangeLabels(" not in src[:src.index("function (TreeArg) {")]
