"""Plain-words summary line of an automation (D8 walk-through): a condition that holds other conditions is told as one clause,
never as its list heading, so the sentence no longer reads "When ... and all of this holds:, then ..."."""
import json
from pathlib import Path

from _board_node import run_node

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "de", "fr", "es", "it", "nl")


def test_composite_conditions_are_one_clause():
    r = run_node("automations_lead_test.js")
    assert r["plain"] == "When the door changes and the door is closed, then the light turns on."
    assert r["all"] == "When the door changes and the door is closed and the lock is locked, then the light turns on."
    assert r["or"] == "When the door changes and (the door is closed or the lock is locked), then the light turns on."
    assert r["not"] == "When the door changes and it is not true that the door is closed, then the light turns on."
    assert r["nested"] == "When the door changes and the door is closed and (the lock is locked or the gate is closed), then the light turns on."
    for text in r.values():
        assert ":," not in text and "holds:" not in text


def test_the_not_clause_exists_in_every_language_with_its_variable():
    for lang in LANGS:
        keys = json.loads((ROOT / "lanloom" / "i18n" / (lang + ".json")).read_text(encoding="utf-8"))
        assert "{parts}" in keys["auto.plain.lead_not"]
