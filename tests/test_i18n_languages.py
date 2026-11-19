"""Languages added after English and German (French, Spanish, Dutch, Italian; O6): the pieces around the two files work for each of them.

The completeness, placeholder and plural checks live in test_i18n_complete.py and run over every file in lanloom/i18n; this file
covers what is specific to the added languages: spelled-out characters, no balloon texts, the AI answer language, the report
renderer and that every language is offered in the language picker."""
import json
import os
import re

import pytest

from lanloom import textfmt
from lanloom.ai import demo, manager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(ROOT, "lanloom", "i18n")
ADDED = ("fr", "es", "nl", "it")


def both(lang):
    out = {}
    for name in ("%s.json", "board.%s.json"):
        with open(os.path.join(I18N, name % lang), encoding="utf-8") as f:
            out.update(json.load(f))
    return out


def test_the_added_languages_are_delivered():
    assert set(ADDED) <= set(textfmt.languages())
    assert sorted(manager.LANG_NAMES) == textfmt.languages()


@pytest.mark.parametrize("lang", ADDED)
def test_texts_are_written_out_and_clean(lang):
    for k, v in both(lang).items():
        assert "\\u" not in v and "�" not in v, "%s %s: escaped or broken character" % (lang, k)
        assert "  " not in v, "%s %s: double blank" % (lang, k)
        assert not re.search(r"[\x00-\x08\x0b-\x1f]", v), "%s %s: control character" % (lang, k)
    # the PDF export writes Western European text: every character of the language has to survive cp1252 or one of the replaced symbols
    from lanloom import pdfmini
    for k, v in both(lang).items():
        if k.startswith(("export.", "plan.", "inv.")):
            assert "?" not in pdfmini.clean(v) or "?" in v, "%s %s has a character outside the PDF's Western European set" % (lang, k)


@pytest.mark.parametrize("lang", ADDED)
def test_short_labels_do_not_balloon(lang):
    """Tiles, buttons and tabs are small: a short English label may not become a long one (the browser check at 390 and 1512 px
    looks at the real screens; this catches the obvious case early)."""
    en = both("en")
    for k, v in both(lang).items():
        e = en[k]
        if len(e) <= 24 and "{" not in e and not k.startswith(("cmdk.kw.", "pc.cmd.")):
            assert len(v) <= 2.3 * len(e) + 6, "%s %s: %r for %r" % (lang, k, v, e)


@pytest.mark.parametrize("lang", ADDED)
def test_search_words_are_lowercase_word_lists(lang):
    for k, v in both(lang).items():
        if k.startswith("cmdk.kw.") or k.endswith("_keywords"):
            assert v == v.lower() and 1 <= len(v.split()) <= 24, "%s %s: %r" % (lang, k, v)


@pytest.mark.parametrize("lang", ADDED)
def test_ai_answer_language_and_demo_answers(lang):
    name = manager.LANG_NAMES[lang]
    system = "You explain ... Answer in %s. At most 120 words." % name
    en = demo.answer("You explain ... Answer in English. At most 120 words.", "Subject: device\nName: Printer\nKind: printer\n")
    got = demo.answer(system, "Subject: device\nName: Printer\nKind: printer\n")
    assert got and got != en, "%s: the demo answer is still English" % lang
    assert "{name}" not in got and "{kind}" not in got


def test_render_dates_and_weekdays_for_every_language():
    from lanloom.nightly import render
    for lang in textfmt.languages():
        assert len(render.WEEKDAYS[lang]) == 7, lang
        assert re.search(r"\d", render.fmt_date("2026-11-22", lang)), lang
