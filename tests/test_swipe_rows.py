"""O15: sideways rows on a phone say that there is more - the edge with content beyond it fades out, a row that fits looks as before.

The behaviour in a real browser (fade only on the side with hidden content, the active chip in view, the phone walk reports an unmarked
sideways scroller) is walked by the private test bench (shots/phone_usable.py, shots/nullwerte.py). Pinned here: the edge logic (LTR and RTL),
the rows that carry the mark, and that the fade is a mask on the row itself (a box beside a measured element counts as overflow)."""
import re
from pathlib import Path

from _board_node import run_node

WEB = Path(__file__).resolve().parents[1] / "lanloom" / "web"


def test_edge_logic_ltr_and_rtl():
    o = run_node("swipe_sides_test.js")
    no = {"left": False, "right": False}
    assert o["fits"] == no and o["subpixel"] == no
    assert o["ltrStart"] == {"left": False, "right": True}
    assert o["ltrMiddle"] == {"left": True, "right": True}
    assert o["ltrEnd"] == {"left": True, "right": False}
    assert o["rtlStart"] == {"left": True, "right": False}      # RTL starts at the right edge: the rest lies to the left
    assert o["rtlMiddle"] == {"left": True, "right": True}
    assert o["rtlEnd"] == {"left": False, "right": True}


def test_every_known_swipe_row_is_marked():
    js = (WEB / "js" / "handy.js").read_text(encoding="utf-8")
    sel = re.search(r"const SWIPE_SEL = '([^']+)'", js).group(1)
    for cls in ("inv-chips", "map-filter", "nm-seg", "fw-draftbar__chips"):
        assert "." + cls in sel, cls
        css = "".join(p.read_text(encoding="utf-8") for p in (WEB / "css").glob("*.css"))
        assert cls in css


def test_fade_is_a_mask_on_the_row_not_a_box_beside_it():
    css = (WEB / "css" / "handy.css").read_text(encoding="utf-8")
    block = css[css.index("[data-swipe][data-swipe-fade]"):]
    assert "mask-image" in block and "--sw-l" in block and "--sw-r" in block
    assert "::after" not in block and "::before" not in block
    js = (WEB / "js" / "handy.js").read_text(encoding="utf-8")
    assert "scrollIntoView" not in js[js.index("swipe rows (O15)"):js.index("keyboard guard")]
