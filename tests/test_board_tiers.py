"""Bus layout of the Network board (board/tiers.js): one box per parent and medium, rows by depth, one line per parent."""
import json
import os
import re

from _board_node import run_node

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lanloom")


def _out():
    if not hasattr(_out, "cache"):
        _out.cache = run_node("tiers_test.js")
    return _out.cache


def test_children_of_one_parent_share_one_box_per_medium():
    out = _out()
    zone_of = out["zone_of"]
    assert {zone_of[i] for i in ("sw2", "ap", "nas", "vh", "pc1", "pc2", "tv")} == {"bus:sw1:cable"}
    assert {zone_of[i] for i in ("pc3", "pc4", "cam1")} == {"bus:sw2:cable"}
    assert {zone_of[i] for i in ("ph1", "ph2", "tab", "iot1", "iot2", "iot3")} == {"bus:ap:radio"}
    assert zone_of["sw1"] == "bus:gw:cable"
    # a device without a known parent stays in its network box; nothing is guessed
    assert zone_of["lone"] == "net:192.168.50.0/24"
    # guests and containers keep their boxes
    assert zone_of["g1"] == zone_of["g2"] == "guests:vh" and zone_of["ct1"] == "containers:nas"


def test_boxes_are_named_by_parent_and_medium_and_sorted_by_depth():
    zones = {z["id"]: z for z in _out()["zones"]}
    assert zones["bus:sw1:cable"]["title_key"] == "zone.bus_lan" and zones["bus:sw1:cable"]["host"] == "Switch A"
    assert zones["bus:ap:radio"]["title_key"] == "zone.bus_wireless" and zones["bus:ap:radio"]["host"] == "Access point"
    assert zones["edge"]["span"] == "full" and zones["edge"]["order"] == 0
    ids = [z["id"] for z in _out()["zones"]]
    assert ids[:3] == ["edge", "bus:gw:cable", "bus:sw1:cable"]
    assert ids.index("bus:sw2:cable") < ids.index("bus:ap:radio") < ids.index("guests:vh") < ids.index("net:192.168.50.0/24")
    assert ids[-1] == "net:192.168.50.0/24"
    # the order inside a box is the order of the server (switch port first)
    assert zones["bus:sw1:cable"]["items"] == ["sw2", "ap", "nas", "vh", "pc1", "pc2", "tv"]


def test_regroup_is_pure_and_dissolves_the_quiet_fold_only():
    out = _out()
    assert out["unchanged"] and out["traces_same"] and out["fields_same"]
    assert out["quiet_tile"] is False and out["quiet_folds"] == 0
    assert out["container_fold"] == "nas"
    assert out["name_option"] == "N:sw1"
    assert out["empty"] == 0


def test_every_depth_starts_a_row_of_its_own():
    out = _out()
    assert out["mixed_rows"] == 0 and out["overlaps"] == 0
    assert out["rows_in_order"] == sorted(out["rows_in_order"])
    assert out["plain_rows"] == 4          # the usual board keeps packing its boxes side by side


def test_children_of_a_switch_hang_on_one_bus_line():
    out = _out()
    bus = out["bus"]
    assert bus["issues"] == [], bus
    assert bus["on_bus"] == bus["kids"] == 7
    assert bus["ways"] == 1                # one way out of the switch for all its children
    assert bus["rows"] == 2 and bus["members"] == [7]
    assert bus["guests"] == 2 and bus["untouched"] and bus["others"] == 2
    assert bus["phone"] is True            # phones keep their rails
    # without the bus line the router alone brings fewer children onto a trunk
    assert out["trunk_bus"]["on"] >= out["trunk_plain"]["on"]


def test_bus_layout_is_wired_into_the_page():
    with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert html.index('src="board/layout.js"') < html.index('src="board/tiers.js"') < html.index('src="board/board.js"')
    with open(os.path.join(ROOT, "web", "js", "views.js"), encoding="utf-8") as f:
        js = f.read()
    assert "LL.Tiers.regroup" in js and "LL.Tiers.busRoutes" in js and "'ll-netbus'" in js and "LL.query.get('bus')" in js
    with open(os.path.join(ROOT, "web", "board", "board.js"), encoding="utf-8") as f:
        assert re.search(r"this\.reshape\(this\.bundle\(routes, spec\)\)", f.read())
    for lang in ("en", "de", "fr", "es", "nl", "it"):
        with open(os.path.join(ROOT, "i18n", lang + ".json"), encoding="utf-8") as f:
            d = json.load(f)
        for k in ("mapfilter.bus", "mapfilter.bus_title", "mapfilter.bus_on", "mapfilter.bus_off", "zone.bus_lan", "zone.bus_wireless"):
            assert d.get(k), (lang, k)
        assert "{host}" in d["zone.bus_lan"] and "{host}" in d["zone.bus_wireless"]
