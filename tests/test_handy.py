"""Phone use with a finger (O12): the hit-area script, the 16 px rule, the keyboard guard and the places that make room themselves.

The behaviour in a real browser (hit areas >= 44 px, nothing covered, every tap reacts, keyboard stand-in) is walked by the private test bench
(shots/phone_usable.py, a step of the overflow test of the gate). What belongs to the product itself is pinned here:
- the page loads css/handy.css last and js/handy.js after the palette, and the self-test knows both files;
- the script has no way out of the page (no request, no storage) and parses;
- fields have 16 px on a phone (phone browsers zoom in below), the layer around a focused field follows the visual viewport, the Ask panel makes room for its history;
- an empty "Send" leads into the field, the site menu closes on a pointer (a touch on a non-clickable place sends no mouse event on many phones)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "lanloom" / "web"


def read(*p):
    return (WEB.joinpath(*p)).read_text(encoding="utf-8")


def test_page_loads_handy_last():
    html = read("index.html")
    css = re.findall(r'<link rel="stylesheet" href="(css/[^"]+)"', html)
    js = re.findall(r'<script src="(js/[^"]+)"', html)
    assert css[-1] == "css/handy.css", "handy.css has to come last: its phone rules win over the page's own sizes"
    assert js.index("js/palette.js") < js.index("js/handy.js")
    main = (ROOT / "lanloom" / "main.py").read_text(encoding="utf-8")
    assert '"js/handy.js"' in main and '"css/handy.css"' in main, "the self-test has to check both assets"


def test_handy_script_stays_in_the_page_and_parses():
    src = read("js", "handy.js")
    for word in ("fetch(", "XMLHttpRequest", "localStorage", "sessionStorage", "WebSocket", "sendBeacon", "navigator."):
        assert word not in src, "the hit-area script and the keyboard guard talk to nothing: " + word
    assert "visualViewport" in src and "ll-kb-layer" in src and "ll-tz" in src
    if shutil.which("node"):
        r = subprocess.run(["node", "--check", str(WEB / "js" / "handy.js")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_phone_rules_in_css():
    css = read("css", "handy.css")
    phone = css[css.index("@media (max-width: 700px)"):]
    assert re.search(r"textarea,\s*select,[^{]*\{\s*font-size:\s*16px\s*!important", phone), "fields need 16 px on a phone, else the phone browser zooms in"
    assert "var(--ll-vvh" in phone and "var(--ll-vvt" in phone, "the layer around a field follows the visual viewport"
    assert "html.ll-kb-open .ask__tools" in phone, "the Ask history keeps its room while the keyboard is up"
    assert ".ll-tz-a::after" in css and ".ll-tz-b::before" in css
    for rule in (".btn", ".seg__btn", ".mf-chip", ".icon-btn"):
        assert rule in phone, rule + " has to make room on a phone"
    assert ":focus" not in css.split("keyboard guard (js/handy.js)")[1], "never tie the layout to :focus of the field (a tap on Send takes the focus away)"


def test_empty_send_leads_into_the_field():
    src = read("js", "ask.js")
    assert re.search(r"if \(!q\) \{ input\.focus\(\); return; \}", src)


def test_site_menu_closes_on_a_pointer():
    src = read("js", "sites.js")
    assert "addEventListener('pointerdown', onDoc)" in src and "mousedown" not in src


def test_firewall_pads_scale_to_a_fingertip_on_the_phone():
    src = read("js", "fwinsight.js")
    assert "{ w: 120, h: 54 }" in src


def test_handbook_names_the_rules():
    doc = (ROOT / "docs" / "ACCESSIBILITY.md").read_text(encoding="utf-8")
    assert "## Phones" in doc and "44" in doc and "16 px" in doc


def test_header_controls_are_a_fingertip_wide_themselves():
    """A hit area beside a control counts as content sticking out of it (scrollWidth > clientWidth in every engine), and the live
    text never fits a circle of 34 px: the header controls are capsules 44 px wide, the live capsule grows with its time, and a side
    that is already 44 px gets no invisible area (1.60.0 and 1.61.0 read every header capsule on a phone as cut)."""
    css = read("css", "handy.css")
    phone = css[css.index("@media (max-width: 700px)"):]
    m = re.search(r"\.topbar \.icon-btn, \.topbar \.ask-pill, \.topbar \.cmdk-pill \{([^}]*)\}", phone)
    assert m and "width: 44px" in m.group(1) and "min-width: 44px" in m.group(1)
    g = re.search(r"\.topbar \.grade-pill,[^{]*\{([^}]*)\}", phone)
    assert g and "width: auto" in g.group(1) and "min-width: 44px" in g.group(1)
    assert "width: 34px" not in phone
    assert ".pw-selchip__x { width: 44px" in phone and ".btn-row > .btn" in phone
    src = read("js", "handy.js")
    assert "r.width >= MIN ? 0" in src and "r.height >= MIN ? 0" in src
