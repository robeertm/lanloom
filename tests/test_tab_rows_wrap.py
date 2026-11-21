"""A row of tabs never reaches past its box and never hides a tab behind a scroll nobody sees (lanloom#10): the style sheets let every
segmented row break into a second line. The real measurement (every view, four widths, two engines) runs in the browser gate; this keeps the rule in the files."""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "lanloom" / "web" / "css"


def rule(sheet, selector):
    text = (CSS / sheet).read_text(encoding="utf-8")
    return [m.group(1) for m in re.finditer(r"(?m)^%s\s*\{([^}]*)\}" % re.escape(selector), text)]


def test_the_segmented_row_wraps_and_stays_in_its_box():
    body = " ".join(rule("firewall.css", ".seg"))
    assert "flex-wrap: wrap" in body and "max-width: 100%" in body
    assert "min(var(--ll-radius-pill)" in body            # a full pill only while the row is one line


def test_the_settings_sections_do_not_hide_behind_a_scroll():
    body = " ".join(rule("access.css", ".settings-subnav") + rule("ai.css", ".settings-subnav"))
    assert "overflow-x" not in body and "flex: none" not in body.replace(".seg__btn", "")
    assert "scrollbar-width: none" not in body
    assert "scrollbar" not in (CSS / "access.css").read_text(encoding="utf-8").split("lanloom#10")[-1].split("}")[0]


def test_the_other_tab_rows_wrap_too():
    assert "flex-wrap: wrap" in " ".join(rule("sensors.css", ".sn-seg"))
    fw = (CSS / "firewall.css").read_text(encoding="utf-8")
    assert not re.search(r"\.fw-source\s*\{[^}]*overflow-x", fw)
