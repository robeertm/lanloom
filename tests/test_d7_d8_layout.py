"""Walk-through findings D7/D8 (phone header, footer, sheets, search, tables, texts): what belongs to the product is pinned here.

The behaviour in a real browser (rows, edges, 44 px, nothing cut) is measured by the private test bench and the layout checker; these are static checks on the shipped files."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "lanloom" / "web"
I18N = ROOT / "lanloom" / "i18n"
LANGS = ("en", "de", "fr", "es", "it", "nl")


def read(*p):
    return WEB.joinpath(*p).read_text(encoding="utf-8")


def keys(lang):
    return json.loads((I18N / (lang + ".json")).read_text(encoding="utf-8"))


def rule(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, selector
    return m.group(1)


def test_site_name_has_its_own_row_on_a_phone():
    css = read("css", "sites.css")
    phone = css[css.index("@media (max-width: 700px)"):]
    assert "flex-wrap: wrap" in rule(phone, ".topbar:has(.site-switch)")
    sw = rule(phone, ".topbar .site-switch")
    assert "flex: 1 0 100%" in sw and "white-space: normal" in sw
    name = rule(phone, ".topbar .site-switch__name")
    assert "text-overflow: clip" in name and "overflow-wrap: anywhere" in name
    assert "grade-pill__text" not in css and "site-switch__pin, .site-switch__caret { display: none" not in css   # the pin and the arrow stay


def test_version_lives_in_the_more_sheet_not_in_a_bar():
    views, now = read("js", "views.js"), read("js", "now.js")
    assert "LL.versionFoot() :" not in views.split("function frame")[1].split("function ")[0] and "verFoot" not in views
    assert "LL.versionFoot ? LL.versionFoot() : null" in now and "body.append(ver)" in now
    more = now[now.index("LL.sheets.register('more'"):]
    assert "full: true" in more[:200], "the More sheet opens at full height: every tile whole"
    assert "LL.whatsNewDot" in now and "LL.whatsNewDot = wnDot" in views


def test_outage_note_is_one_line_on_a_phone_with_details_on_demand():
    js, css = read("js", "failure.js"), read("css", "failure.css")
    assert "fc-outage__more" in js and "'aria-expanded'" in js
    phone = css[css.index("@media (max-width: 760px)"):]
    assert "text-overflow: ellipsis" in rule(phone, ".fc-outage:not(.is-open) .fc-outage__text")
    assert "display: none" in rule(phone, ".fc-outage:not(.is-open) .fc-outage__tools > .btn:not(.btn--icon)")
    assert "display: none" in rule(css, ".fc-outage__more")


def test_countries_come_after_the_devices_and_carry_the_globe():
    pal, country = read("js", "palette.js"), read("js", "paths_country.js")
    assert "late: true, icon: 'globe'" in country
    assert "globe:" in pal and "x.cmd.late" in pal
    assert pal.index("GROUP_ORDER.slice(1).forEach") < pal.index("add('countries'")
    assert "(r.cmd && r.cmd.icon)" in pal


def test_traffic_plot_shows_a_placeholder_until_its_first_answer():
    js = read("js", "history.js")
    assert "tplot__skel" in js and "t('plot.loading')" in js
    i = js.index("if (!data || data.range !== range)")
    body = js[i:i + 700]
    assert "skel.hidden = false" in body and "skel.hidden = true" in body


def test_sources_dialog_table_wraps_and_close_buttons_are_44px():
    assert "lg-table" in read("js", "views.js")
    lg = rule(read("css", "app.css"), ".lg-table th, .lg-table td")
    assert "white-space: normal" in lg and "overflow-wrap: anywhere" in lg
    handy = read("css", "handy.css")
    close = re.search(r"\.sheet__close[^{]*\{([^}]*)\}", handy)
    assert close and "min-width: 44px" in close.group(1) and "min-height: 44px" in close.group(1)
    for sel in (".modal__head .icon-btn", ".detail__close", ".pc-close", ".nm-full__close", ".pw-full__close"):
        assert sel in handy


def test_automation_search_placeholder_is_short_in_every_language():
    assert "placeholder: t('auto.search_ph')" in read("js", "automations.js")
    for lang in LANGS:
        k = keys(lang)
        assert len(k["auto.search_ph"]) <= 36, lang
        assert len(k["auto.search_ph"]) < len(k["auto.search"]), lang


def test_wide_filter_bar_wraps_and_settings_tabs_fill_their_rows():
    ov = read("css", "overview.css")
    wide = ov[ov.index("@media (min-width: 701px)"):]
    assert "flex-wrap: wrap" in rule(wide, ".map-filter")
    acc = read("css", "access.css")
    assert "flex: 1 1 auto" in rule(acc, ".settings-head .settings-subnav .seg__btn")
    assert ".settings-subnav" in re.search(r"SWIPE_SEL = '([^']*)'", read("js", "handy.js")).group(1)


def test_webhook_table_breaks_addresses_at_dots_and_keeps_events_under_the_name():
    js, css = read("js", "apipage.js"), read("css", "api.css")
    assert "function breakAtDots" in js and "h('wbr')" in js and "api-evs" in js
    assert "api.hooks.events" not in js[js.index("acc-row--head"):js.index("acc-row--head") + 200]
    assert "overflow-wrap: break-word" in rule(css, ".api-table--hooks .acc-row > span.api-target")
    assert "white-space: nowrap" in rule(css, ".api-table .api-cell--when .chip")


def test_audit_detail_and_sensors_show_words_not_raw_identifiers():
    acc, sn = read("js", "access.js"), read("js", "sensors.js")
    assert "const dKey" in acc and "LL.fmtDate(v)" in acc and "1e9" in acc
    assert "' · ' + d.name" not in sn and "sn-row__sub mono" not in sn and "sn-ent__id" not in sn
    for lang in LANGS:
        k = keys(lang)
        for name in ("scans_keep", "live_min", "paths_hours", "start", "end", "days", "bytes", "format"):
            assert "audit.d." + name in k, (lang, name)


def test_failure_chain_sheet_leaves_the_card_head_free_on_a_phone():
    """With a card open, the chain sheet starts where the card's head ends: the card's title and cross stay in view and can be tapped
    (fixed at 62 % of the screen the sheet covered the cross once the header grew a row for the site name)."""
    app, css = read("css", "app.css"), read("css", "failure.css")
    phone_app = app[app.index("@media (max-width: 700px)"):]
    assert "--ll-detail-phone-h: 78%" in rule(phone_app, ".main")
    assert "height: var(--ll-detail-phone-h)" in rule(phone_app, ".detail")
    phone = css[css.rindex("@media (max-width: 700px)"):]
    bar = rule(phone, ".main:has(> .detail.is-open) .fc-bar")
    assert "position: absolute" in bar and "bottom: 0" in bar
    m = re.search(r"calc\(var\(--ll-detail-phone-h, 78%\) - (\d+)px\)", bar)
    assert m and int(m.group(1)) >= 18 + 44 + 10 + 1, "the sheet must not reach into the card's head (padding + 44 px cross + border)"


def test_whats_new_dot_sits_at_the_text_of_a_44px_link():
    """The dot of "What's new?" sits at the top of the text line: the link is a 44 px fingertip on a phone, a dot at top: 1px floated above it."""
    app = read("css", "app.css")
    dot = rule(app, ".ver-foot__link .wn-dot")
    assert "top: calc(50% - .75em)" in dot and "top: 1px" not in dot
