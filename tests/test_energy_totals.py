"""Energy (lanloom/energy.py): sums, means and doubles among the power sensors do not become consumers, the whole-home figure,
the choice of a person (home sensor, hidden sensors) and the names of the lanes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crhelp import T0, FakeCore, ent, hub_entry   # noqa: E402
from lanloom.energy import EnergyBook             # noqa: E402


def book(rows, devices=(), areas=()):
    """rows: [(entity_id, name, watts, device_id or None, area_id or None)]"""
    core = FakeCore()
    ents, st = [], {}
    for eid, name, w, dev, area in rows:
        e = ent(eid, dev, name, "W", "power")
        e["area"] = area
        ents.append(e)
        st[eid] = (str(w), T0)
    entry = hub_entry(ents, st, [{"id": i, "name": n} for i, n in devices], t=T0)
    entry["snapshot"]["areas"] = [{"id": i, "name": n} for i, n in areas]
    core.hub = {1: entry}
    return core, EnergyBook(core, clock=lambda: T0)


TESTER = [
    ("sensor.total_power", "Total Power", 311, None, None),
    ("sensor.total_power_1m", "Total Power 1m", 318, None, None),
    ("sensor.circuit_pc_power", "Circuit PC Power", 230, None, None),
    ("sensor.office_plug_power", "Power", 230, "d-pc", "office"),
    ("sensor.circuit_server_network_power", "Circuit Server Network Power", 80, None, None),
    ("sensor.lr_a_power", "Power", 30, "d-a", "living"),
    ("sensor.lr_b_power", "Power", 22, "d-b", "living"),
    ("sensor.lr_c_power", "Power", 14, "d-c", "living"),
    ("sensor.lr_d_power", "Power", 6.9, "d-d", "living"),
]
DEVS = [("d-pc", "PC plug"), ("d-a", "TV plug"), ("d-b", "Lamp plug"), ("d-c", "Router plug"), ("d-d", "Charger plug")]
AREAS = [("office", "Office"), ("living", "Living Room")]


def make_tester():
    return book(TESTER, DEVS, AREAS)


def test_the_total_is_the_home_and_nothing_is_counted_twice():
    core, b = make_tester()
    v = b.view(T0)
    assert v["now"]["home_w"] == 311.0
    assert v["home_source"] == {"kind": "sensor", "id": "sensor.total_power", "label": "Total Power", "marked": False}
    ids = [x["id"] for x in v["lanes"]]
    assert "sensor.total_power" not in ids and "sensor.total_power_1m" not in ids
    assert [x["w"] for x in v["lanes"] if x["w"] == 230.0] == [230.0]
    assert "sensor.office_plug_power" in ids and "sensor.circuit_pc_power" not in ids
    assert "sensor.circuit_server_network_power" not in ids        # a circuit sums the plugs below it: never on top of them
    assert sorted(ids) == ["sensor.lr_a_power", "sensor.lr_b_power", "sensor.lr_c_power", "sensor.lr_d_power",
                           "sensor.office_plug_power"]
    why = {x["id"]: x["why"] for x in v["skipped"]}
    assert why == {"sensor.total_power_1m": "average", "sensor.circuit_pc_power": "duplicate",
                   "sensor.circuit_server_network_power": "circuit"}
    assert sum(x["w"] for x in v["lanes"]) <= v["now"]["home_w"] and v["rest_w"] == round(311.0 - 302.9, 1)


def test_lane_labels_are_device_names():
    v = make_tester()[1].view(T0)
    labels = {x["id"]: x["label"] for x in v["lanes"]}
    assert labels["sensor.office_plug_power"] == "PC plug"
    assert sorted(labels[i] for i in labels if i.startswith("sensor.lr_")) == ["Charger plug", "Lamp plug", "Router plug", "TV plug"]
    assert all(x["label"] != "Power" for x in v["lanes"])


def test_label_rules():
    from lanloom.energy import label_of
    row = {"entity_id": "sensor.x", "name": "Channel 2 power", "device_name": "Dual plug"}
    assert label_of(row) == "Dual plug · Channel 2 power"
    assert label_of(dict(row, name="Dual plug power")) == "Dual plug power"
    assert label_of(dict(row, name="Dual plug")) == "Dual plug"
    assert label_of(dict(row, name="", device_name="")) == "sensor.x"
    assert label_of(dict(row, name="Current power")) == "Dual plug"


def test_rest_is_home_minus_counted_lanes():
    v = make_tester()[1].view(T0)
    assert v["rest_w"] == round(max(0.0, 311.0 - sum(x["w"] for x in v["lanes"])), 1)
    core, b = book([("sensor.t", "Total power", 500, None, None), ("sensor.a", "Power", 100, "d", None)], [("d", "Plug")])
    v = b.view(T0)
    assert v["now"]["home_w"] == 500.0 and v["rest_w"] == 400.0 and v["lanes"][0]["label"] == "Plug"


def test_average_variant_alone_is_still_the_total():
    core, b = book([("sensor.t1", "Total power 5min avg", 300, None, None), ("sensor.a", "Power", 100, "d", None)], [("d", "Plug")])
    v = b.view(T0)
    assert v["now"]["home_w"] == 300.0 and v["home_source"]["id"] == "sensor.t1"


def test_marked_home_entity_wins():
    core, b = make_tester()
    b.set_settings({"home_entity": "sensor.circuit_pc_power"})
    v = b.view(T0)
    assert v["now"]["home_w"] == 230.0 and v["home_source"]["marked"] is True and v["home_source"]["id"] == "sensor.circuit_pc_power"
    assert "sensor.circuit_pc_power" not in [x["id"] for x in v["lanes"]]
    assert v["settings"]["home_entity"] == "sensor.circuit_pc_power"
    b.set_settings({"home_entity": None})
    assert b.view(T0)["now"]["home_w"] == 311.0


def test_hidden_sensor_does_not_show_and_does_not_count():
    core, b = make_tester()
    b.set_settings({"hidden": ["sensor.lr_a_power", "sensor.gone_power"]})
    v = b.view(T0)
    assert "sensor.lr_a_power" not in [x["id"] for x in v["lanes"]]
    hid = {x["id"]: x for x in v["skipped"] if x["why"] == "hidden"}
    assert hid["sensor.lr_a_power"]["w"] == 30.0 and hid["sensor.gone_power"]["w"] is None
    core, b = book([("sensor.a", "Power", 100, "d1", None), ("sensor.b", "Power", 50, "d2", None)], [("d1", "One"), ("d2", "Two")])
    assert b.view(T0)["now"]["home_w"] == 150.0
    b.set_settings({"hidden": ["sensor.b"]})
    assert b.view(T0)["now"]["home_w"] == 100.0


def test_two_sensors_of_one_device_count_once():
    core, b = book([("sensor.p", "Power", 40, "d", None), ("sensor.p1", "Power 1m", 38, "d", None), ("sensor.q", "Power", 10, "e", None)],
                   [("d", "Heater"), ("e", "Lamp")])
    v = b.view(T0)
    assert v["now"]["home_w"] == 50.0 and sorted(x["id"] for x in v["lanes"]) == ["sensor.p", "sensor.q"]
    assert [x["why"] for x in v["skipped"]] == ["average"]
    core, b = book([("sensor.p", "Power", 40, "d", None), ("sensor.p2", "Power meter", 40.5, "d", None)], [("d", "Heater")])
    v = b.view(T0)
    assert v["now"]["home_w"] == 40.5 and [x["id"] for x in v["skipped"]] == ["sensor.p"] and v["skipped"][0]["why"] == "duplicate"


def test_channels_of_one_device_all_count():
    core, b = book([("sensor.p", "Channel A power", 40, "d", None), ("sensor.pb", "Channel B power", 25, "d", None)], [("d", "Dual plug")])
    v = b.view(T0)
    assert v["now"]["home_w"] == 65.0 and v["skipped"] == [] and len(v["lanes"]) == 2


def test_the_own_total_of_a_device_is_that_device_never_the_home():
    rows = [("sensor.strip_total", "Total power", 150, "s", None), ("sensor.strip_1", "Channel 1 power", 100, "s", None),
            ("sensor.strip_2", "Channel 2 power", 50, "s", None), ("sensor.pc", "Power", 230, "d", None)]
    core, b = book(rows, [("s", "Power strip"), ("d", "PC plug")])
    v = b.view(T0)
    assert v["home_source"]["kind"] == "lanes" and v["now"]["home_w"] == 380.0
    assert sorted(x["id"] for x in v["lanes"]) == ["sensor.pc", "sensor.strip_total"]
    assert {x["id"]: x["why"] for x in v["skipped"]} == {"sensor.strip_1": "duplicate", "sensor.strip_2": "duplicate"}


def test_a_sum_below_a_single_consumer_is_no_home_total():
    core, b = book([("sensor.t", "Total power", 100, None, None), ("sensor.a", "Power", 230, "d", None)], [("d", "PC plug")])
    v = b.view(T0)
    assert v["home_source"]["kind"] == "lanes" and v["now"]["home_w"] == 230.0 and v["skipped"][0]["why"] == "total"


def test_circuits_alone_are_lanes():
    core, b = book([("sensor.circuit_a", "Circuit kitchen power", 300, None, None), ("sensor.circuit_b", "Circuit office power", 120, None, None)])
    v = b.view(T0)
    assert v["now"]["home_w"] == 420.0 and len(v["lanes"]) == 2 and v["skipped"] == []


def test_circuit_equal_to_the_sum_of_its_room():
    core, b = book([("sensor.circuit_office_power", "Circuit Office", 100, None, "o"), ("sensor.a", "Power", 60, "d1", "o"),
                    ("sensor.b", "Power", 41, "d2", "o")], [("d1", "One"), ("d2", "Two")], [("o", "Office")])
    v = b.view(T0)
    assert v["now"]["home_w"] == 101.0 and [x["why"] for x in v["skipped"]] == ["circuit"]


def test_grid_sensor_keeps_the_old_behaviour():
    rows = [("sensor.grid_power", "Grid power", 400, None, None), ("sensor.solar_power", "Solar power", 600, None, None),
            ("sensor.total_power", "Total Power", 999, None, None), ("sensor.a", "Power", 200, "d", None)]
    core, b = book(rows, [("d", "Plug")])
    v = b.view(T0)
    assert v["now"]["home_w"] == 1000.0 and v["home_source"]["kind"] == "grid"
    assert v["grid_share"] == 0.4
    assert [x["id"] for x in v["lanes"]] == ["sensor.a"] and v["skipped"][0]["why"] == "total"
    assert v["lanes"][0]["cost_h"] is None or v["lanes"][0]["share"] == 0.2


def test_home_from_lanes_without_any_total():
    core, b = book([("sensor.a", "Power", 100, "d1", None), ("sensor.b", "Power", 50, "d2", None)], [("d1", "One"), ("d2", "Two")])
    v = b.view(T0)
    assert v["home_source"]["kind"] == "lanes" and v["now"]["home_w"] == 150.0 and v["rest_w"] == 0.0 and v["skipped"] == []


def test_old_stored_settings_still_load():
    core, b = make_tester()
    core.store.set_setting("energy", {"price": 0.3, "currency": "EUR"})
    s = b.settings()
    assert s["price"] == 0.3 and s["home_entity"] is None and s["hidden"] == []
    core.store.set_setting("energy", {"hidden": "oops", "home_entity": 5})
    s = b.settings()
    assert s["hidden"] == [] and s["home_entity"] is None


def test_settings_validation():
    core, b = make_tester()
    with pytest.raises(ValueError, match="energy.err.entity"):
        b.set_settings({"home_entity": "sensor.nope"})
    for bad in ("sensor.a", [5], ["bad id"], ["sensor.x"] * 201, [None], [{"a": 1}]):
        with pytest.raises(ValueError, match="energy.err.hidden"):
            b.set_settings({"hidden": bad})
    assert b.set_settings({"hidden": ["sensor.a", "sensor.a", "sensor.b"]})["hidden"] == ["sensor.a", "sensor.b"]
    assert b.set_settings({"hidden": None})["hidden"] == []
    assert b.set_settings({"home_entity": "sensor.total_power"})["home_entity"] == "sensor.total_power"
