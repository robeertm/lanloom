"""Firewall board (web, js/fwinsight.js): geometry and small decisions under Node, wiring in the page, and the rules the
front end has to keep (read only, every text through t(), colours only from tokens, nothing without an accessible name)."""
import os
import re

from _board_node import run_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "lanloom", "web")


def read(*p):
    with open(os.path.join(WEB, *p), encoding="utf-8") as f:
        return f.read()


def test_messages_nest():
    r = run_node("fwinsight_test.js")
    assert r["version"] == "1.0.0"
    # a variable that is a message is resolved first, numbers stay numbers (plural forms need them)
    assert r["plain"] == "a(n=3,src=b(x=1,y=c()))"
    assert r["notes"] == ["n1()", "n2(z=q)"]
    assert r["plainNone"] == ["", "x", "", "k()", [], []]


def test_board_layout_fits_two_to_twelve_zones():
    r = run_node("fwinsight_test.js")["layouts"]
    for mode in ("wide", "narrow"):
        for row in r[mode]:
            assert row["count"] == row["n"], (mode, row)
            assert row["inside"], (mode, row)            # every pad lies inside the drawing
            assert row["clash"] == 0, (mode, row)        # no two pads touch (6 units apart at least)
            assert row["same"], (mode, row)              # the same zones always give the same board
    assert {x["w"] for x in r["wide"]} == {960} and {x["w"] for x in r["narrow"]} == {380}
    # the phone board grows with the zones, the wide one is a fixed size up to 12
    assert r["narrow"][12]["h"] > r["narrow"][4]["h"]
    assert {x["h"] for x in r["wide"][2:9]} == {500} and {x["h"] for x in r["wide"][9:13]} == {560}


def test_edges_arrows_and_numbers():
    e = run_node("fwinsight_test.js")["edge"]
    assert e["sides"] == [1, 1] and e["opposite"]       # a line and the one back never lie on each other
    assert e["startOutside"] and e["endOutside"] and e["startOnBorder"]
    assert e["arrowTip"] and e["arrowPoints"] == 3 and e["pathShape"]
    assert e["straightUp"] == [True, True, True] and e["samePoint"] == [True, True]
    assert e["curveEnds"] == [True, True]
    assert e["labels"] == {"middle": True, "moved": True, "none": None}


def test_width_by_hits_and_kinds():
    r = run_node("fwinsight_test.js")
    h = r["hit"]
    assert h["rising"] and h["lo"] == 1.4 and h["hi"] == 4.4 and h["widths"][0] == 1.4
    assert h["noMax"] == 1.4 and h["over"] == 4.4 and h["custom"] == 5 and h["none"] == 1.4
    # allowed or hit -> solid; blocked traffic seen -> red dashed; both can be drawn; nothing seen and not allowed -> list only
    assert r["kinds"] == [["allow"], [], ["allow"], ["block"], ["allow", "block"]]


def test_checks_ports_and_questions():
    r = run_node("fwinsight_test.js")
    assert r["summary"][0] == {"total": 0, "worst": None}
    assert r["summary"][1] == {"total": 2, "worst": "warn"} and r["summary"][2] == {"total": 3, "worst": "alert"}
    assert r["summary"][3] == {"total": 0, "worst": None} and r["summary"][4] == {"total": 1, "worst": "info"}
    assert r["sorted"] == [2, 4, 3, 1]                   # worst first, the order inside a level stays
    ok = [p["ok"] for p in r["ports"]]
    assert ok == [True, True, True, True, True, False, True, False, False, False, False, False, False]
    assert [p["port"] for p in r["ports"]][:5] == [None, None, None, 443, 80]
    assert r["query"][0] == "/api/firewall/ask?source=int%3A5&from=internet&to=zone%3Az_a&proto=tcp&port=443"
    assert r["query"][1] == "/api/firewall/ask?from=a%20b&to=n_1"
    assert r["query"][2] == "/api/firewall/ask?source=x&from=a&to=b"      # an unknown protocol and a port out of range are left out
    assert r["query"][3].endswith("&proto=udp&port=53")


def test_wired_into_the_page():
    assert '<script src="js/fwinsight.js"></script>' in read("index.html")
    assert '<link rel="stylesheet" href="css/fwinsight.css">' in read("index.html")
    assert read("index.html").index("js/fwinsight.js") > read("index.html").index("js/firewall.js")
    assert read("index.html").index("css/fwinsight.css") > read("index.html").index("css/firewall.css")
    with open(os.path.join(ROOT, "lanloom", "main.py"), encoding="utf-8") as f:
        main = f.read()
    assert '"js/fwinsight.js"' in main and '"css/fwinsight.css"' in main
    fw = read("js", "firewall.js")
    assert "/api/firewall/insight?source=" in fw and "LL.fwInsightCards(" in fw
    assert "checksShown" in fw                           # the old checks card gives way to the one with advice
    assert "id: 'firewall-ask'" in read("js", "fwinsight.js")


def test_read_only_and_accessible():
    js = read("js", "fwinsight.js")
    # nothing here may change anything: only GET questions
    assert not re.search(r"method\s*:\s*'(POST|PUT|DELETE|PATCH)'", js)
    assert js.count("LL.api(") == 2                      # the question and the palette command (both GET)
    # the drawing is hidden from screen readers where it only repeats the list, and every pad is a named button
    assert "role: 'button'" in js and "'aria-label': t(" in js and "'aria-hidden': 'true'" in js
    assert "<details" not in js and "h('details'" in js and "h('caption', { class: 'sr-only' }" in js
    # the keyboard can do what the mouse does
    assert "ev.key === 'Enter' || ev.key === ' '" in js
    assert "tabindex: '0'" in js


def test_css_uses_only_tokens():
    css = read("css", "fwinsight.css")
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css), "colours come from tokens.css"
    assert not re.findall(r"rgba?\(", css)
    tokens = read("css", "tokens.css")
    for name in set(re.findall(r"var\((--ll-[\w-]+)", css)):
        assert re.search(re.escape(name) + r"\s*:", tokens), name
