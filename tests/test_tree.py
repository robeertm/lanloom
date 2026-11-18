"""Tree view of the physical chain (accessibility twin of the board): pure logic under Node, assets are shipped, the module is English-only,
and the keyboard help names every key the tree answers to."""
import json
import os
import re

from _board_node import run_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "lanloom", "web")


def read(*p):
    with open(os.path.join(WEB, *p), encoding="utf-8") as f:
        return f.read()


def test_chain_is_built_from_the_physical_links_only():
    r = run_node("tree_test.js")
    assert r["rootIds"] == ["net"]                                             # the Internet is the only top
    assert r["depthIds"][:4] == ["1:net", "2:gw", "3:sw", "4:p2"]
    assert r["otherIds"] == ["lone"]                                           # no physical link: listed apart, not lost
    assert r["count"] == 10 and r["problems"] == 2                             # 9 in the chain + 1 apart; a down device and an interrupted one
    assert r["swCount"] == 6                                                   # everything below the switch, nested ones counted
    assert r["pcLink"]["kind"] == "cable" and r["pcLink"]["port"] == "5"
    assert r["apChild"]["kind"] == "radio" and r["apChild"]["band"] == "5" and r["apChild"]["ssid"] == "Home"
    assert r["broken"] == 1 and r["empty"] == 0                                # a link to a node that is not there, a link to itself, no data


def test_loops_and_flow_lines_do_not_make_a_chain():
    r = run_node("tree_test.js")
    assert "cam" in r["switchChildren"]                                        # the loop trace (cam -> sw) did not turn the switch into a child of the camera
    assert r["depthIds"].count("3:sw") == 1 and not any(x.endswith(":lone") for x in r["depthIds"])


def test_ports_are_in_natural_order():
    r = run_node("tree_test.js")
    assert r["switchChildren"] == ["p2", "ap", "pc", "cam", "p10"]             # port 2, 4, 5, 6, 10: numbers, not letters
    assert r["natural"] == ["1", "2", "9", "10", "a"]


def test_filters_keep_the_way_up_to_the_internet():
    r = run_node("tree_test.js")
    assert r["problemsOnly"] == ["net", "gw", "sw", "ap", "ph", "cam"]        # the two problems with their chain; healthy siblings are gone
    assert r["zoneMobile"] == ["net", "gw", "sw", "ap", "ph"]


def test_assets_are_shipped_and_loaded():
    html = read("index.html")
    for rel in ("board/tree.js", "js/a11y.js", "css/tree.css", "css/a11y.css"):
        assert os.path.isfile(os.path.join(WEB, rel)), rel
        assert rel in html, rel
    assert html.index("js/a11y.js") < html.index("js/history.js")             # the helpers exist before the views ask for them
    assert 'aria-live' not in re.search(r'<div id="app"[^>]*>', html).group(0)  # the page is not one big live region


def test_tree_module_is_english_only_and_has_no_literals_for_people():
    src = read("board/tree.js")
    assert not re.search(r"[äöüÄÖÜß]", src)
    # every text a person reads goes through t(...); the only literals are separators and symbols
    for m in re.finditer(r"el\('(?:span|p|li|div)', '[\w -]+', '([^']+)'\)", src):
        assert not re.search(r"[A-Za-z]{3,}", m.group(1)), m.group(0)


def test_keyboard_help_names_every_key_of_the_tree():
    src = read("board/tree.js")
    help_src = read("js/a11y.js")
    keys = set(re.findall(r"case '(\w+|\*| )'", src))
    assert {"ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End", "Enter", "*"} <= keys
    for key in ("up", "right", "left", "home", "end", "enter", "space", "star"):
        assert "kbd('%s'" % key in help_src, key
    en = json.loads(open(os.path.join(ROOT, "lanloom", "i18n", "en.json"), encoding="utf-8").read())
    for k in re.findall(r"'(a11y\.help\.[\w]+)'", help_src):
        assert k in en, k
