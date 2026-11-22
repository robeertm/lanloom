"""Importable colour themes (lanloom/web/js/usertheme.js): strict value check, allow-list, contrast warnings, CSS form, wiring."""
import re
from pathlib import Path

from _board_node import run_node

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "lanloom" / "web"


def _aurora_light_keys():
    css = (WEB / "css" / "palettes.css").read_text(encoding="utf-8")
    m = re.search(r'\n\[data-palette="aurora"\],\n.*?\{(.*?)\n\}', css, re.S)
    return [k for k in re.findall(r"(--[\w-]+)\s*:", m.group(1))]


def test_allow_list_equals_the_palette_tokens():
    r = run_node("usertheme_test.js")
    assert r["tokens"] == _aurora_light_keys()


def test_shipped_palettes_pass_the_strict_check():
    r = run_node("usertheme_test.js")
    assert r["shippedPalettes"] and r["shippedBad"] == [], "an export of any shipped palette must be importable again"


def test_valid_theme_is_accepted_and_bad_ones_refused():
    r = run_node("usertheme_test.js")
    assert r["valid"]["ok"] and r["valid"]["name"] == "Test theme (1)" and r["valid"]["n"] == 66
    for name, res in r["refused"].items():
        assert res["ok"] is False, name
    assert r["refused"]["oversize"]["code"] == "size" and r["refused"]["badName"]["code"] == "name"
    for k in ("url", "semicolon", "styleTag", "varRef", "expression", "quote", "comment"):
        assert r["refused"][k]["code"] == "value", k


def test_unknown_tokens_are_dropped_and_reported():
    r = run_node("usertheme_test.js")
    assert r["unknown"]["ok"] and r["unknown"]["dropped"] == ["--evil", "background-image"] and not r["unknown"]["hasEvil"]
    assert r["unknown"]["text"] == "#101010"
    assert r["fill"]["light"] == r["fill"]["all"] == r["fill"]["dark"] and r["fill"]["bg"]


def test_css_has_only_allowed_tokens_and_the_exact_selector_forms():
    r = run_node("usertheme_test.js")
    css = r["css"]
    assert r["cssTokens"] == [] and r["cssBad"] == [] and r["badId"]
    sel = '[data-palette="custom"]'
    assert sel + ',\n' + sel + ' [data-theme="light"]:not([data-palette]) {' in css
    assert sel + '[data-theme="dark"],\n' + sel + ' [data-theme="dark"]:not([data-palette]) {' in css
    assert '@media (prefers-color-scheme: dark) {\n    ' + sel + '[data-theme="auto"],\n  ' + sel + ' [data-theme="auto"]:not([data-palette]) {' in css
    assert r["exportRound"] == {"ok": True, "name": "Weird name here", "same": True}


def test_contrast_warnings():
    r = run_node("usertheme_test.js")
    assert r["contrast"]["bw"] == 21 and r["contrast"]["same"] == 1
    assert r["goodWarnings"] == 0
    assert r["poor"]["n"] > 0 and r["poor"]["kinds"] == ["contrast", "surface"] and r["poor"]["modes"] == ["dark", "light"]


def test_wiring():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert html.index("js/usertheme.js") < html.index("js/app.js")
    assert '"js/usertheme.js"' in (ROOT / "lanloom" / "main.py").read_text(encoding="utf-8")
    views = (WEB / "js" / "views.js").read_text(encoding="utf-8")
    assert "LL.usertheme" in views and "type: 'file'" in views and "URL.createObjectURL" in views
    assert "LL.paletteName" in views and "LL.paletteName" in (WEB / "js" / "palette.js").read_text(encoding="utf-8")
    js = (WEB / "js" / "usertheme.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
    app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "ll-user-theme" in app and "textContent = LL.usertheme.toCss" in app and "innerHTML" not in app[app.index("LL.applyUserTheme"):app.index("LL.applyUserTheme") + 600]
