"""The shared vocabulary of the control room (levels.py) and the reading of hub entities (hubfacts.py)."""
import pytest

from crhelp import T0, FakeCore, ent, hub_entry
from lanloom.controlroom import hubfacts, levels


def test_worst_orders_none_ok_unknown_warn_danger():
    assert levels.worst([]) == "none"
    assert levels.worst(["ok"]) == "ok"
    assert levels.worst(["ok", "unknown"]) == "unknown"                           # an unknown next to ok is not ok
    assert levels.worst(["unknown", "warn", "ok"]) == "warn"
    assert levels.worst(["warn", "danger", "ok"]) == "danger"
    assert levels.worst(["none", "ok"]) == "ok"
    assert levels.worst(["bogus", "ok"]) == "ok"                                  # a level nobody knows never wins
    assert levels.worst(iter(["ok", "danger"])) == "danger"


@pytest.mark.parametrize("age,expect", [(None, "none"), (0, "fresh"), (180, "fresh"), (180.1, "late"), (900, "late"), (900.1, "stale"), (86400, "stale")])
def test_freshness_limits(age, expect):
    assert levels.freshness(age) == expect


def test_freshness_with_own_limits():
    assert levels.freshness(50, 60, 120) == "fresh" and levels.freshness(100, 60, 120) == "late" and levels.freshness(121, 60, 120) == "stale"


@pytest.mark.parametrize("value,expect", [(1, 1.0), ("2.5", 2.5), (0, 0.0), (-3, -3.0), (None, None), ("", None), ("x", None), (float("nan"), None),
                                          (float("inf"), None), (1e15, None), (1e14, 1e14), ([1], None), (True, 1.0)])
def test_num(value, expect):
    assert levels.num(value) == expect


@pytest.mark.parametrize("p,level", [(None, "unknown"), (0, "ok"), (84.9, "ok"), (85, "warn"), (94.9, "warn"), (95, "danger"), (100, "danger")])
def test_pct_level(p, level):
    assert levels.pct_level(p) == level


def test_pct_level_with_own_limits():
    assert levels.pct_level(50, 40, 60) == "warn" and levels.pct_level(60, 40, 60) == "danger"


def test_item_shape():
    assert levels.item("cr.x", "warn", n=3) == {"key": "cr.x", "vars": {"n": 3}, "level": "warn"}
    assert levels.item("cr.x") == {"key": "cr.x", "vars": {}, "level": "ok"}


# ---------------------------------------------------------------------------------------------------- hub facts
def test_fold_lowercases_and_drops_accents_and_punctuation():
    assert hubfacts.fold("Last Successful  Backup!") == "last successful backup"
    assert hubfacts.fold("Größe-Prüfung_ÄÖ") == "grosse prufung ao"
    assert hubfacts.fold(None) == "" and hubfacts.fold(5) == "5"
    assert hubfacts.fold("sensor.lte_rsrp") == "sensor lte rsrp"


def test_as_number_accepts_comma_and_refuses_the_rest():
    assert hubfacts.as_number("7,5") == 7.5 and hubfacts.as_number(" 3 ") == 3.0 and hubfacts.as_number(4) == 4.0
    for junk in (None, "", "unknown", "nan", "inf", "1e99", "12 GB"):
        assert hubfacts.as_number(junk) is None


def test_entities_join_names_states_and_devices():
    core = FakeCore()
    core.hub = {7: hub_entry([ent("sensor.a_rsrp", "dev1", "RSRP", "dBm"), ent("light.x", None, "Lamp"), {"entity_id": 5, "name": "bad"}, {"name": "no id"}],
                             {"sensor.a_rsrp": ("-90", T0 - 5)}, [{"id": "dev1", "name": "Router"}], t=T0 - 60)}
    out = list(hubfacts.entities(core))
    assert [e["entity_id"] for e in out] == ["sensor.a_rsrp", "light.x"]
    a, x = out
    assert a["integ"] == 7 and a["device"] == "dev1" and a["device_name"] == "Router" and a["state"] == "-90" and a["t"] == T0 - 5 and a["unit"] == "dBm"
    assert a["domain"] == "sensor" and "rsrp" in a["text"]
    assert x["device_name"] == "" and x["state"] is None and x["t"] == T0 - 60                      # no state yet: the snapshot's time


def test_entities_of_a_core_without_hubs_or_snapshots():
    assert list(hubfacts.entities(FakeCore())) == []
    core = FakeCore()
    core.hub = {1: {"snapshot": None, "states": None}, 2: {}}
    assert list(hubfacts.entities(core)) == []
    assert list(hubfacts.entities(object())) == []
