"""3D view of the chain (O7): the pure layout under Node, the assets are shipped, the module is English-only, the view falls back
to the flat board without graphics acceleration, and every key the scene answers to is in the keyboard help."""
import json
import os
import re

from _board_node import run_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "lanloom", "web")
I18N = os.path.join(ROOT, "lanloom", "i18n")


def read(*p):
    with open(os.path.join(*p), encoding="utf-8") as f:
        return f.read()


def test_one_layer_per_step_of_the_chain():
    r = run_node("scene3d_test.js")
    assert r["layers"]["net"] == 0 and r["layers"]["gw"] == 1
    assert r["layers"]["sw"] == 2 and r["layers"]["ap"] == 2                 # switches and access points share a layer
    assert all(r["layers"][x] == 3 for x in ("pc", "ph", "nas", "vm", "lone"))
    assert r["layers"]["sw2"] == 3                                             # a switch behind a device never climbs above it
    assert r["layerY"] == sorted(r["layerY"], reverse=True)                   # the Internet on top, the devices at the bottom
    assert r["onBoard"] and r["distinct"]                                     # every chip on its board, no two on one spot


def test_traces_drop_through_a_via_to_the_board_below():
    r = run_node("scene3d_test.js")
    assert r["linkIds"] == ["p:1", "p:2", "p:3", "p:4", "p:5", "p:6", "p:7", "u:gw"]   # only physical links and the uplink; no flow line
    p3 = r["p3"]
    assert p3["a"] == "sw" and p3["b"] == "pc"                                # a is the part above: dots use the trace direction of the board
    assert p3["first"] == r["p3sw"]                                           # starts at the switch
    assert abs(p3["last"][1] - 0.012) < 1e-9 and [p3["last"][0], p3["last"][2]] == r["p3pc"]   # ends on the board at the device
    assert r["along0"] == p3["first"] and r["along1"] == p3["last"]
    assert r["radioKind"] == "radio"


def test_filters_keep_the_way_up():
    r = run_node("scene3d_test.js")
    assert r["problems"] == ["ap", "gw", "net", "ph", "sw"]


def test_software_renderers_are_not_graphics_acceleration():
    r = run_node("scene3d_test.js")
    assert r["gpu"] == [True, True, False, False, False, True]


def test_camera_and_projection():
    r = run_node("scene3d_test.js")
    assert r["centre"] == [800, 450]                                          # the target of the orbit camera is the middle of the screen
    assert r["behind"] is None                                                # nothing behind the eye is projected
    c = r["colors"]
    assert [round(x * 255) for x in c[0][:3]] == [12, 133, 116] and c[0][3] == 1
    assert [round(x * 255) for x in c[1][:3]] == [170, 187, 204]
    assert abs(c[2][3] - 0.14) < 1e-9 and c[3] == [1, 2, 3, 4]


def test_dots_are_capped_and_carried():
    r = run_node("scene3d_test.js")
    a, b, c = r["dots"]
    assert a["n"] == 3 and abs(a["carry"] - 0.5) < 1e-9 and not a["big"]
    assert b["n"] == 14 and b["big"] and b["carry"] <= 400                    # never more than the cap, the carry stays bounded
    assert c["n"] == 1                                                        # fractions add up over the seconds


def test_large_network_stays_square_and_quick():
    r = run_node("scene3d_test.js")
    assert r["bigNodes"] == 250 and r["bigLinks"] == 249 and r["bigDistinct"]
    assert 0.5 < r["bigAspect"] < 2.0                                         # the device board is roughly square, not a long strip
    assert r["bigMs"] < 1500
    assert r["empty"] == 0


def test_assets_are_shipped_and_loaded_after_the_tree():
    html = read(WEB, "index.html")
    for rel in ("board/scene3d.js", "css/scene3d.css"):
        assert os.path.isfile(os.path.join(WEB, rel)), rel
        assert rel in html, rel
    assert html.index("board/tree.js") < html.index("board/scene3d.js")       # the layout builds on the tree's chain


def test_module_is_english_only_and_has_no_text_for_people():
    src = read(WEB, "board", "scene3d.js")
    assert not re.search(r"[äöüÄÖÜß]", src)
    # every text a person reads goes through t(...): el() with a text argument only gets a t() call, a title or a symbol
    for m in re.finditer(r"el\('[a-z]+', '[^']*', '([^']+)'\)", src):
        assert not re.search(r"[A-Za-z]{2}", m.group(1)), m.group(0)


def test_view_falls_back_to_the_board_without_acceleration():
    views = read(WEB, "js", "views.js")
    assert "LL.Scene3D.available()" in views and "s3d.fallback" in views and "s3d.try_anyway" in views
    assert "LL.Scene3D.canTry()" in views                                    # "Try anyway" only where WebGL exists at all
    src = read(WEB, "board", "scene3d.js")
    assert "failIfMajorPerformanceCaveat: true" in src                       # the browser itself says when it would be slow
    assert "webglcontextlost" in src                                          # a lost context goes back to the board


def test_frame_budget_follows_the_motion_setting():
    src = read(WEB, "board", "scene3d.js")
    assert "const MOTION = { auto: 30, full: 60, saver: 15, off: 0 }" in src   # the same steps as the dots of the flat board (B4)
    assert "prefers-reduced-motion" in src and "IntersectionObserver" in src and "doc.hidden" in src


def test_every_key_is_in_the_keyboard_help_and_all_languages():
    a11y = read(WEB, "js", "a11y.js")
    for key in ("a11y.help.s3d_turn", "a11y.help.s3d_tilt", "a11y.help.s3d_zoom", "a11y.help.s3d_reset", "a11y.help.s3d_walk"):
        assert key in a11y
    src = read(WEB, "board", "scene3d.js")
    for k in ("'ArrowLeft'", "'ArrowUp'", "'PageDown'", "'PageUp'", "'Home'", "'End'", "'Enter'", "'0'", "'+'", "'-'"):
        assert k in src, k
    en = json.loads(read(I18N, "en.json"))
    keys = [k for k in en if k.startswith(("s3d.", "netview.3d", "netview.now_3d", "a11y.help.s3d", "cmdk.kw.network3d"))]
    assert len(keys) >= 25
    for lang in ("de", "fr", "es", "nl", "it"):
        other = json.loads(read(I18N, lang + ".json"))
        assert all(other.get(k) for k in keys), lang


def test_palette_has_a_command_for_the_3d_view():
    pal = read(WEB, "js", "palette.js")
    assert "'#/network?v=3d'" in pal
    views = read(WEB, "js", "views.js")
    assert "FACES.includes(asked)" in views                                  # ?v=3d opens the 3D face directly
