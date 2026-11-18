"""One switch for the four faces of the network (Live | Plan | Firewall | Data paths), the tags and the network filter of the
devices list, the network toggles of the board legend, the fold tile of a large network, the full screen of the map and the
port-noise setting: the pure helpers under Node and the wiring into the pages, index.html, the self test and the asset lists."""
import os
import re

from _board_node import run_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "lanloom", "web")


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def test_modes_and_tags():
    r = run_node("netmodes_test.js")
    assert r["modes"] == [["live", "#/network"], ["plan", "#/firewall/plan"], ["firewall", "#/firewall"], ["paths", "#/paths"]]
    # the key of a network: its name, else its VLAN number, else none (a device without tags has none)
    assert r["keys"] == ["net:Home", "net:Home", "net:Guests", "vlan:30", "net:Lab", "", "net:Home"]
    # options: named networks first (sorted), then those known by VLAN only; wireless names sorted; "wired" only when a device is wired
    assert r["options"] == {"nets": [["net:Guests", "Guests", 20], ["net:Home", "Home", 70], ["net:Lab", "Lab", None], ["vlan:30", "", 30]],
                            "wireless": ["Casa", "Guest"], "wired": True}
    assert r["none"] == {"nets": [], "wireless": [], "wired": False}
    # which of the seven devices each filter value keeps (a device without tags matches only "all")
    assert r["match"] == ["1111111", "0101000", "1100001", "0001000", "1000101", "0000100", "0000000"]
    # a value from the address that no option offers counts as "all"
    assert r["clean"] == ["", "wired", "net:Home", "", "wireless:Casa", "", "vlan:30", ""]
    assert r["cleanNoWired"] == ""
    # chips: VLAN (the network name rides along as title), the network name when there is no VLAN, wireless with a short band
    assert r["chips"][0] == [{"kind": "vlan", "v": 70, "title": "Home"}, {"kind": "wireless", "ssid": "Casa", "band": "5"}]
    assert r["chips"][1] == [{"kind": "net", "name": "Lab"}]
    assert r["chips"][3][1] == {"kind": "wireless", "ssid": "Guest", "band": ""}
    assert r["chips"][5] == []


def test_legend_network_toggles():
    r = run_node("netmodes_test.js")["toggles"]
    # name, else "VLAN <n>", else the title
    assert run_node("netmodes_test.js")["label"] == ["Home", 'board.legend.vlan{"v":"20"}', "c", "Other network", "e"]
    assert r["one"] == [] and r["none"] == []                       # a single network needs no toggle
    assert [x["id"] for x in r["mixed"]] == ["net:a", "net:b", "net:c"]    # no edge, cabling, guests or folded zone; board order
    assert r["active"] == [["net:a", False], ["net:b", True]]
    assert r["inactive"] == [["net:a", True], ["net:b", True]]
    assert r["other"] == [["net:a", False], ["net:b", False]]


def test_assets_are_registered():
    html = read("lanloom", "web", "index.html")
    assert 'href="css/netmodes.css"' in html and 'src="js/netmodes.js"' in html
    # after the sheet that defines .seg, before the view that uses the switch
    assert html.index("css/firewall.css") < html.index("css/netmodes.css")
    assert html.index("js/netmodes.js") < html.index("js/views.js") < html.index("js/firewall.js") < html.index("js/paths.js")
    main = read("lanloom", "main.py")
    assert '"js/netmodes.js"' in main and '"css/netmodes.css"' in main
    for rel in ("js/netmodes.js", "css/netmodes.css"):
        assert os.path.isfile(os.path.join(WEB, rel))


def test_switch_sits_on_the_three_pages():
    views, fw, paths = read("lanloom", "web", "js", "views.js"), read("lanloom", "web", "js", "firewall.js"), read("lanloom", "web", "js", "paths.js")
    assert "LL.netModes('live')" in views
    assert "LL.netModes(onPlan ? 'plan' : 'firewall')" in fw and "rest[0] === 'plan'" in fw
    assert "LL.netModes('paths')" in paths
    # the links are real anchors with aria-current
    nm = read("lanloom", "web", "js", "netmodes.js")
    assert "h('a', { href" in nm and "'aria-current': k === active ? 'page' : null" in nm


def test_quiet_fold_wiring():
    views = read("lanloom", "web", "js", "views.js")
    # the tile toggles the fold of its network instead of opening a card; unfolded it reads "Fold N quiet devices"
    assert "startsWith('qm:')" in views and "LL.setUnfolded(qm.fold_key, !LL.isUnfolded(qm.fold_key))" in views
    assert "'comp.quiet.less'" in views and "c.vars || {}" in views
    # the tree has no fold tile
    assert "c => !c.quiet_more" in views
    # the board draws it as a toggle: aria-expanded, no status wording
    board = read("lanloom", "web", "board", "board.js")
    assert "aria-expanded" in board and "if (c.quiet_more) { e.el.setAttribute('aria-label'" in board


def test_devices_filter_and_settings_wiring():
    views = read("lanloom", "web", "js", "views.js")
    assert "LL.deviceNetSelect" in views and "LL.query.get('net')" in views and "LL.deviceTags" in views
    assert "keep(d) && keepNet(d)" in views
    assert "port_noise: noise.checked" in views and "s.port_noise" in views and "'settings.port_noise.hint'" in views
    # full screen of the map: only in board mode, moves the host
    assert "LL.hostFullScreen" in views and "mode === 'board' && fullScreen ? fullButton()" in views


def test_texts_of_the_switch_are_keys():
    """Every text of netmodes.js is a key (the languages merge them into en.json / de.json; test_i18n_complete checks that they exist)."""
    nm = read("lanloom", "web", "js", "netmodes.js")
    used = set(re.findall(r"'((?:nm|devices\.tag|devices\.filter)\.[a-z_.0-9]+)'", nm))
    assert {"nm.label", "nm.live", "nm.plan", "nm.full.open", "nm.full.close", "nm.full.aria", "devices.tag.vlan", "devices.tag.wireless", "devices.filter.label",
            "devices.filter.all", "devices.filter.g_net", "devices.filter.g_wireless", "devices.filter.net_vlan", "devices.filter.wireless", "devices.filter.wired"} <= used
