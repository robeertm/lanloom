"""Board legend as line filter (issue 6): the line styles in the legend are toggle buttons; a hidden kind is left out of
the drawing, the dots, the highlight and the end and port marks, and the choice is kept per view in the browser."""
import os

from _board_node import run_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, "lanloom", "web", "board", "board.js")
KINDS = ["uplink", "lan", "service", "flow", "integration", "cable", "radio", "virtual"]


def src():
    with open(BOARD, encoding="utf-8") as f:
        return f.read()


def test_pure_helpers():
    r = run_node("legend_filter_test.js")
    assert r["keys"] == KINDS + ["unmeasured"]
    # stored values: unknown keys dropped, broken or foreign values mean "nothing hidden"
    assert r["parse"] == [["cable", "radio"], ["radio"], [], [], [], [], []]
    # kindFilter(): null = all; a hidden kind is missing; "unmeasured" is no kind of its own
    assert r["visible"][0] is None
    assert r["visible"][1] == sorted(k for k in KINDS if k != "radio")
    assert r["visible"][2] == sorted(KINDS)
    assert r["visible"][3] == sorted(k for k in KINDS if k not in ("radio", "flow"))
    # traces: hidden by kind, unmeasured ones by "unmeasured", unknown kinds count as LAN like in the drawing
    assert r["shown"] == [True, False, True, False, True, False, True, False]
    assert r["version"] == "1.1.0"


def test_filter_sits_in_every_drawing_path():
    s = src()
    assert "kindFilter: () => null" not in s
    assert "kindFilter: () => visibleKinds(this.hidden)" in s
    # the live redraw paths (renderTraces, dots, marks, highlight) all ask the filter, not only CSS
    for needle in ("if (!t || !traceShown(t, this.hidden)) return;",               # renderTraces + traceViews
                   "if (!exitShown(x, this.hidden)) return;",                       # exits in renderTraces
                   "this.exits.filter(x => exitShown(x, this.hidden))",             # exits for the marks
                   "if (!traceShown(t, this.hidden)) return;",                      # dot geometry
                   "traceShown(this.traceInfo.get(r.id), this.hidden)"):            # highlight
        assert needle in s, needle
    assert s.count("traceShown(t, this.hidden)") >= 3


def test_legend_toggles_are_buttons_with_state():
    s = src()
    assert 'data-flt="${key}" aria-pressed="${on}"' in s
    assert "data-flt-all" in s and "board.legend.show_all" in s
    assert "lanloom.board.hide." in s
    assert 'class="ll-legend" role="group"' in s
    # live frames only change the dot size text, they never rebuild the legend (a focused toggle keeps the focus)
    assert "this.frameQ = frame.q; this.updateQLegend();" in s
    with open(os.path.join(ROOT, "lanloom", "web", "css", "board.css"), encoding="utf-8") as f:
        css = f.read()
    assert ".ll-legend__flt.is-off span { text-decoration: line-through; }" in css
    assert ".ll-legend__flt:focus-visible" in css
