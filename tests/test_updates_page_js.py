"""Settings > Updates is a page of its own (lanloom#1, last comment).

The Updates card used to be one of many cards on Settings > General and sank lower with every new section. It now has
its own tab at ``#/settings/updates`` (administrators), the "What's new" window leads there (and to the update dialog
when a newer version waits), and the command palette has *Open Settings > Updates*. The browser files have no Node
build, so they are read as text, like tests/test_palette.py does.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "lanloom" / "web" / "js"
LANGS = ("en", "de", "es", "fr", "it", "nl")
NEW_KEYS = ("whatsnew.to_updates", "whatsnew.update_now", "cmdk.kw.updates")


def js(name):
    return (WEB / name).read_text(encoding="utf-8")


def _function(src, head):
    start = src.index(head)
    nxt = re.search(r"\n  (?:async )?function |\n  LL\.\w+ = ", src[start + len(head):])
    return src[start:start + len(head) + (nxt.start() if nxt else len(src))]


def test_updates_is_a_settings_tab_for_admins_before_sites():
    src = js("views.js")
    pages = src[src.index("const SETTINGS_PAGES"):src.index("LL.settingsPages = SETTINGS_PAGES")]
    tabs = re.findall(r"\['([a-z]+)', '([a-z]*)', '([a-z_.]+)', '([a-z]+)'\]", pages)
    assert ("updates", "updates", "settings.updates", "admin") in tabs
    order = [k for k, *_ in tabs]
    assert order.index("updates") == order.index("sites") - 1 and order[-1] == "sites"      # Sites stays the last tab


def test_the_sub_page_map_names_the_updates_page_for_admins_only():
    view = _function(js("views.js"), "async function settingsView(")
    assert re.search(r"updates: LL\.can\('admin'\) && updatesPage", view)


def test_general_no_longer_builds_the_updates_card():
    view = _function(js("views.js"), "async function settingsView(")
    assert "updCard" not in view and "/api/update'" not in view
    page = _function(js("views.js"), "async function updatesPage(")
    assert "api('/api/update')" in page and "api('/api/settings')" in page
    assert "pg.append(updCard(upd))" in page and "setUpdateState(upd)" in page
    assert "u.mode === 'addon'" in page                                                     # the add-on branch moved along


def test_whats_new_leads_to_the_updates_page_for_admins():
    wn = _function(js("views.js"), "async function openWhatsNew(")
    assert "whatsnew.to_updates" in wn and "whatsnew.update_now" in wn
    assert "LL.go('#/settings/updates')" in wn and "openUpdate()" in wn
    assert "LL.can('admin') ?" in wn                                                        # no footer for viewers and operators
    assert re.search(r"const d = dialog\(t\('whatsnew\.title'\), .*, foot\);", wn)


def test_the_palette_opens_settings_updates():
    src = js("palette.js")
    settings = src[src.index("const SETTINGS = "):src.index("SETTINGS.forEach")]
    assert "['updates', '/updates', 'settings.updates', 'admin']" in settings
    check = src[src.index("id: 'update-check'"):src.index("id: 'whatsnew'")]
    assert "LL.go('#/settings/updates')" in check and "LL.go('#/settings')" not in check


def test_the_new_texts_exist_in_every_language():
    en = json.loads((ROOT / "lanloom" / "i18n" / "en.json").read_text(encoding="utf-8"))
    for lang in LANGS:
        d = json.loads((ROOT / "lanloom" / "i18n" / (lang + ".json")).read_text(encoding="utf-8"))
        for k in NEW_KEYS:
            assert d.get(k, "").strip(), (lang, k)
            if lang != "en" and not k.startswith("cmdk.kw."):
                assert d[k] != en[k], (lang, k)


def test_the_manual_has_the_updates_page():
    settings = (ROOT / "docs" / "manual" / "settings.md").read_text(encoding="utf-8")
    assert "| [Updates](#updates) | `#/settings/updates` | administrators |" in settings and "\n### Updates\n" in settings
    updating = (ROOT / "docs" / "manual" / "updating.md").read_text(encoding="utf-8")
    assert "General > Updates" not in updating and "settings.md#updates" in updating
