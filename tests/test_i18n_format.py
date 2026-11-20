"""Plural forms and number formatting: the JavaScript side (js/textfmt.js, run under Node) and the Python side (lanloom/textfmt.py,
used for the texts of delivered night reports) give the same text, and the plural forms of the real translations work."""
import json
import os
import shutil
import subprocess

import pytest

from lanloom import textfmt

HERE = os.path.dirname(os.path.abspath(__file__))

TEXTS = [
    ["{n, plural, one {# device} other {# devices}}", {"n": 1}, "en"],
    ["{n, plural, one {# device} other {# devices}}", {"n": 0}, "en"],
    ["{n, plural, one {# device} other {# devices}}", {"n": 1234}, "en"],
    ["{n, plural, one {# Gerät} other {# Geräte}}", {"n": 1234567}, "de"],
    ["{n, plural, =0 {nothing} one {# thing} other {# things}} left", {"n": 0}, "en"],
    ["{n, plural, =0 {nichts} one {# Ding} other {# Dinge}} übrig", {"n": 2}, "de"],
    ["{r} of {n, plural, one {# runs} other {# run}} on {nodes, plural, one {# node} other {# nodes}}", {"r": 3, "n": 1, "nodes": 2}, "en"],
    ["{n, plural, one {once} other {# times}}, {dur} in total", {"n": 5, "dur": "2 min"}, "en"],
    ["{n, plural, one {once} other {# times}}", {}, "en"],
    ["plain {a} and {b}", {"a": 1}, "en"],
    ["{n, plural, one {# thing {x} needs} other {# things need}} a look", {"n": 1, "x": "X"}, "en"],
    ["{n, plural, one {# item} other {# items}}", {"n": 1.5}, "en"],
    ["{n, plural, one {# appareil} other {# appareils}}", {"n": 0}, "fr"],
    ["{n, plural, one {# appareil} other {# appareils}}", {"n": 1.5}, "fr"],
    ["{n, plural, one {# appareil} other {# appareils}}", {"n": 12345}, "fr"],
    ["{n, plural, one {# dispositivo} other {# dispositivos}}", {"n": 0}, "es"],
    ["{n, plural, one {# dispositivo} other {# dispositivos}}", {"n": 1234}, "es"],
    ["{n, plural, one {# dispositivo} other {# dispositivos}} {m, plural, one {# vez} many {# de veces} other {# veces}}", {"n": 1234567, "m": 2000000}, "es"],
    ["{n, plural, one {# apparaat} other {# apparaten}}", {"n": 1234}, "nl"],
    ["{n, plural, one {# dispositivo} other {# dispositivi}}", {"n": 1}, "it"],
    ["{n, plural, one {# dispositivo} other {# dispositivi}}", {"n": 12345.5}, "it"],
]
NUMS = [["en", 1234.5, None], ["de", 1234.5, None], ["de", 0.5, 1], ["en", 12, 0], ["de", 1234567.891, 2], ["de", 12.0, None], ["en", -3.25, None], ["de", 7, 0],
        ["fr", 1234.5, None], ["fr", 1234567.891, 2], ["fr", 0.5, 1], ["es", 1234.5, None], ["es", 12345.5, None], ["es", 1234567, 0], ["es", 999, 0],
        ["nl", 1234.5, None], ["nl", 0.5, 1], ["it", 1234.5, None], ["it", 12345.5, None], ["it", 1234567.891, 2], ["it", -1234, 0]]


LANGS_CHECKED = ("en", "de", "fr", "es", "nl", "it")


def run_node(texts, nums):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, os.path.join(HERE, "js", "format_test.js"), json.dumps(texts), json.dumps(nums)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-1500:]
    return json.loads(proc.stdout)


def norm(s):
    # Intl writes a narrow no-break space or none depending on the ICU build; the numbers here never contain one
    return s.replace(" ", " ").replace(" ", " ")


def test_javascript_and_python_agree():
    js = run_node(TEXTS, NUMS)
    for (text, vars_, lang), got in zip(TEXTS, js["fill"]):
        assert norm(got) == norm(textfmt.fill(text, vars_, lang)), (text, vars_, lang, got)
    for (lang, n, d), got in zip(NUMS, js["num"]):
        want = textfmt.num(lang, n, d)
        if lang in textfmt.MIN_GROUPING and len(str(int(abs(n)))) == 4:
            # Spanish and Italian write four digits without a separator in current CLDR data (minimumGroupingDigits 2); the ICU of an
            # older Node (the NAS test image) still groups them. Both are right for their data: only the separator may differ.
            assert norm(got).replace(".", "") == norm(want).replace(".", ""), (lang, n, d, got)
            continue
        assert norm(got) == norm(want), (lang, n, d, got)


def test_plural_categories_match_cldr_table():
    js = run_node([], [])
    assert len(js["cat"]) == len(LANGS_CHECKED)
    for lang, row in zip(LANGS_CHECKED, js["cat"]):
        want = [textfmt.category(lang, n) for n in (0, 1, 2, 5, 21, 1000000)]
        assert row[:5] == want[:5], lang
        # "many" (exact millions) came with newer CLDR data; an older ICU says "other" there. Texts need no "many" form, so both are fine.
        assert row[5] == want[5] or (row[5] == "other" and want[5] == "many"), lang


def test_board_fallback_matches_for_english():
    js = run_node([], [])
    assert js["fallback"] == ["1 tile → X", "2 tiles → X", "7 tiles → X"]


def test_german_decimal_comma_and_grouping():
    assert textfmt.num("de", 1234.5) == "1.234,5"
    assert textfmt.num("en", 1234.5) == "1,234.5"
    assert textfmt.num("de", 0.5, 1) == "0,5"
    assert textfmt.num("de", 12, 0) == "12"
    assert textfmt.fill("{n, plural, one {# Gerät} other {# Geräte}}", {"n": 1}, "de") == "1 Gerät"
    assert textfmt.fill("{n, plural, one {# Gerät} other {# Geräte}}", {"n": 2000}, "de") == "2.000 Geräte"


def test_report_renderer_writes_numbers_in_the_reader_language():
    from lanloom.nightly import render
    assert render.fmt_bytes(1_234_567, "de") == "1,2 MB"
    assert render.fmt_bytes(1_234_567, "en") == "1.2 MB"
    r = render.Renderer("de", None, "2026-01-01")
    assert r.values({"pct": 99.5, "n": 3})["pct"] == "99,5"
    assert r.values({"pct": 99.5, "n": 3})["n"] == 3
    assert r.tr("report.head", n=1) == "1 Ding braucht einen Blick"
    assert r.tr("report.head", n=4) == "4 Dinge brauchen einen Blick"
    assert render.Renderer("en", None, "2026-01-01").tr("report.head", n=1) == "1 thing needs a look"


def test_every_language_has_its_rules():
    """A new language file needs its plural rule, required plural forms and number separators in lanloom/textfmt.py."""
    for lang in textfmt.languages():
        assert lang in textfmt.PLURAL_RULES and lang in textfmt.PLURAL_CATEGORIES and lang in textfmt.SEPARATORS, lang
        assert lang in LANGS_CHECKED, "add %s to LANGS_CHECKED here and to the cat list in tests/js/format_test.js" % lang


def test_french_spanish_dutch_italian_numbers_and_plurals():
    assert textfmt.num("fr", 1234.5) == "1\u202f234,5"
    assert textfmt.num("es", 1234.5) == "1234,5" and textfmt.num("es", 12345) == "12.345"
    assert textfmt.num("it", 1234) == "1234" and textfmt.num("it", 1234567) == "1.234.567"
    assert textfmt.num("nl", 1234.5) == "1.234,5"
    one = "{n, plural, one {# appareil} other {# appareils}}"
    assert textfmt.fill(one, {"n": 0}, "fr") == "0 appareil"     # French: zero is singular
    assert textfmt.fill(one, {"n": 2}, "fr") == "2 appareils"
    assert textfmt.fill("{n, plural, one {# apparaat} other {# apparaten}}", {"n": 0}, "nl") == "0 apparaten"
    assert textfmt.fill("{n, plural, one {# dispositivo} other {# dispositivi}}", {"n": 1}, "it") == "1 dispositivo"
