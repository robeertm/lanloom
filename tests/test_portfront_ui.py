"""Front view of a switch (faceplate): the layout of the pads (one row up to eight ports, otherwise odd numbers on top and even
below, wrapping into bands on a narrow plate, SFP cages as their own group), the short speed labels, the 24-hour wording, and the
wiring into the firewall tab, the device card, the palette and the asset lists."""
import os
import re

from _board_node import run_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "lanloom", "web")


def read(*p):
    with open(os.path.join(WEB, *p), encoding="utf-8") as f:
        return f.read()


def test_layout_rows_and_bands():
    r = run_node("portfront_test.js")
    one = lambda n: {"single": True, "bands": [{"cols": n, "rows": [list(range(1, n + 1))]}], "sfp": []}
    assert r["small"] == one(5) and r["eight"] == one(8)
    assert r["empty"] == {"single": True, "bands": [], "sfp": []}
    # more than eight ports: odd numbers on the top row, even below
    assert r["sixteen"]["bands"] == [{"cols": 8, "rows": [[1, 3, 5, 7, 9, 11, 13, 15], [2, 4, 6, 8, 10, 12, 14, 16]]}]
    assert r["sixteen"]["single"] is False
    # a narrow plate wraps into bands that keep the odd/even pairing; the last band may be shorter
    assert r["sixteenNarrow"]["bands"] == [{"cols": 4, "rows": [[1, 3, 5, 7], [2, 4, 6, 8]]}, {"cols": 4, "rows": [[9, 11, 13, 15], [10, 12, 14, 16]]}]
    assert r["twentyFour"]["bands"][1] == {"cols": 4, "rows": [[17, 19, 21, 23], [18, 20, 22, 24]]}
    # eight ports on a plate that cannot hold eight columns use two rows too
    assert r["eightNarrow"]["bands"] == [{"cols": 4, "rows": [[1, 3, 5, 7], [2, 4, 6, 8]]}]
    # SFP cages are a separate group; the copper ports around them are numbered as they are
    assert r["withSfp"]["sfp"] == [15, 16]
    assert r["withSfp"]["bands"][0]["rows"] == [[1, 3, 5, 7, 9, 11, 13], [2, 4, 6, 8, 10, 12, 14]]
    # ports come in any order and are sorted by their number
    assert r["shuffled"]["bands"][0]["rows"] == [[1, 3, 5, 7, 9], [2, 4, 6, 8, 10]]


def test_wording_and_sizes():
    r = run_node("portfront_test.js")
    # short speed on a pad: 100 for Mbit/s, G from a gigabit up; nothing for unknown or nonsense
    assert r["speeds"] == ["10", "100", 'pf.speed.g{"n":"1"}', 'pf.speed.g{"n":"2.5"}', 'pf.speed.g{"n":"10"}', 'pf.speed.g{"n":"25"}', "", "", "", ""]
    assert r["speedsFull"] == ['pf.speed.mbit{"n":"100"}', 'pf.speed.gbit{"n":"1"}', 'pf.speed.gbit{"n":"2.5"}', ""]
    # the level is a word plus a symbol (never colour alone); unknown values count as ok
    assert r["levelKeys"] == ["pf.level.ok", "pf.level.warn", "pf.level.bad", "pf.level.ok", "pf.level.ok"]
    assert r["marks"] == ["", "!", "!!", "", ""]
    assert r["health"] == ["pf.calm", 'pf.flaps{"n":3}', 'pf.errors{"n":2}', 'pf.flaps{"n":1}, pf.errors{"n":1}']
    # columns that fit a width (pads stay at least 30 px wide); the SFP group goes below a narrow plate
    assert r["cols"][0] == [12, 12] and r["cols"][1] == [8, 8] and r["cols"][5] == [24, 24]
    assert all(c[0] >= 4 for c in r["cols"])
    assert r["beside"] == [True, False, False, True, True]


def test_wiring():
    html, main = read("index.html"), open(os.path.join(ROOT, "lanloom", "main.py"), encoding="utf-8").read()
    assert html.index("js/portfront.js") < html.index("js/views.js")
    assert html.index("css/firewall.css") < html.index("css/portfront.css")
    assert '"js/portfront.js"' in main and '"css/portfront.css"' in main
    fw, views, pf = read("js", "firewall.js"), read("js", "views.js"), read("js", "portfront.js")
    assert "id: 'fw-eq-card'" in fw and "eqPorts(e," in fw and "LL.portFront(f, { head: false })" in fw
    assert "'ll-fw-focus') === 'equipment'" in fw
    assert "d.portfront && LL.portFront" in views and "t('pf.title')" in views and "nd.portfront" in views
    assert "id: 'switch-ports'" in pf and "sessionStorage.setItem('ll-fw-focus', 'equipment')" in pf


def test_accessible_and_themed():
    pf, css = read("js", "portfront.js"), read("css", "portfront.css")
    # pads are real buttons with a spoken label, the plate is a group, the decoration is hidden from readers
    assert "h('button', { type: 'button', class: 'pf-pad'" in pf and "'aria-label': padLabel(p)" in pf
    assert "aria-pressed" in pf and "aria-expanded" in pf and "role: 'group'" in pf
    assert "'ArrowRight'" in pf and "'ArrowDown'" in pf and "'Escape'" in pf
    # the selection lives outside the element, so a redraw of the page keeps the open panel
    assert "const selected = new Map();" in pf
    # colours only from tokens: no hex or rgb colour in the file, and every var() names a token defined in tokens.css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    tokens = set(re.findall(r"(--[\w-]+)\s*:", read("css", "tokens.css")))
    local = set(re.findall(r"(--pf-[\w-]+)\s*:", css))
    used = set(re.findall(r"var\((--[\w-]*\w)", css))
    assert used <= tokens | local | {"--pf-cols"}, used - tokens - local      # --pf-cols is set per band by the script
    # the plate follows the mode like every other surface (no dark island on a light page, see the palette check of the
    # gate): surface and border tokens that carry the theme, never a token that stays dark in the light theme
    mapping = dict(re.findall(r"(--pf-[\w-]+):\s*var\((--[\w-]+)\)", css))
    assert mapping["--pf-plate"].startswith("--ll-surface") and mapping["--pf-plate-line"] == "--ll-border-strong"
    assert not any("pcb-silk" in v for v in mapping.values())
    assert len(re.findall(r"--pf-plate\s*:", css)) == 1                   # one mapping for every mode
    # the health mark is a symbol, not only a colour
    assert ".pf-health" in css and "levelMark(lv)" in pf


def test_signal_block_is_drawn_for_access_points_only_and_reads_without_colour_alone():
    pf, css = read("js", "portfront.js"), read("css", "portfront.css")
    assert "if (sigBox) root.append(sigBox)" in pf and "isAp && front.signal ? signalBlock(front.signal) : null" in pf
    # a bar carries a spoken summary and the counts as digits; a clinging device is a sentence with a mark, not only a colour
    assert "role: 'img', 'aria-label': label" in pf and "role: 'note'" in pf and "t('pf.sig.stuck'" in pf
    for sel in (".pf-sig__seg--good", ".pf-sig__seg--fair", ".pf-sig__seg--weak", ".pf-sig__stuck"):
        assert sel in css
