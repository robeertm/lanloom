"""Guards for three small fixes found by the walk-through (static checks on the shipped files, no browser).

* The phone's "More" list names a view by the label of its tab, never by the tab's title: a tab with an unread mark carries that mark as
  its title, so the list once read "1 unread" where "Reports" belongs. The mark is its own element.
* A plain `.btn` (no variant class) has a background and a text colour of its own; before, the browser's own button colours showed
  (light grey with light text in the dark theme, e.g. "Show the chain" in the outage note).
* The start message "not reachable" heals itself: the page asks again while the message is shown.
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "lanloom" / "web"


def test_more_list_uses_the_tab_label_not_the_title():
    src = (WEB / "js" / "now.js").read_text(encoding="utf-8")
    assert "moreLabel(a)" in src and "'.tab__label'" in src
    assert "a.getAttribute('title') || a.textContent" not in src
    assert "more-list__badge" in src
    assert ".more-list__badge" in (WEB / "css" / "now.css").read_text(encoding="utf-8")


def test_plain_button_has_its_own_colours():
    css = (WEB / "css" / "app.css").read_text(encoding="utf-8")
    base = re.search(r"^\.btn \{([^}]*)\}", css, re.M | re.S)
    assert base, "the .btn rule is missing"
    assert "background:" in base.group(1) and "color:" in base.group(1)


def test_boot_error_asks_again_by_itself():
    src = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    i = src.index("t('error.offline')")
    assert "bootRetry = setTimeout" in src[i:i + 900]
