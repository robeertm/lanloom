"""Battery backup (lanloom/controlroom/ups.py) and the UPS-MIB reading of an SNMP agent (lanloom/sources/snmp.parse_ups_mib)."""
import pytest

from crhelp import T0, FakeCore
from lanloom.controlroom import ups as ups_mod
from lanloom.controlroom.ups import UpsBook, state_of
from lanloom.sources import snmp


# ---------------------------------------------------------------------------------------------------- status words
@pytest.mark.parametrize("status,state", [
    ("OL", "online"), ("ol", "online"), ("OL CHRG", "charging"), ("OB", "battery"), ("OB DISCHRG", "battery"), ("DISCHRG", "battery"),
    ("OB LB", "low_battery"), ("LB", "low_battery"), ("OL LB", "low_battery"), ("RB", "replace"), ("OL RB", "replace"), ("OVER", "overload"),
    ("OL OVER", "overload"), ("BYPASS", "online"), ("OL BOOST", "online"), ("OL TRIM", "online"), ("OL,CHRG", "charging"), ("OL ALARM", "online"),
    ("", "unknown"), (None, "unknown"), ("   ", "unknown"), ("OFF", "unknown"), ("WHAT", "unknown"),
])
def test_status_words(status, state):
    assert state_of(status) == state


def test_on_battery_with_little_charge_or_runtime_is_low():
    assert state_of("OB", charge=100.0) == "battery"
    assert state_of("OB", charge=30.0) == "low_battery"                      # default limit 30 %
    assert state_of("OB", charge=29.0, charge_low=20.0) == "battery"
    assert state_of("OB", charge=20.0, charge_low=20.0) == "low_battery"
    assert state_of("OB", charge=90.0, runtime_s=120.0) == "low_battery"       # about to run out
    assert state_of("OB", charge=90.0, runtime_s=121.0) == "battery"
    assert state_of("OL", charge=5.0) == "online"                              # on mains the charge does not matter here


def test_levels_of_the_states():
    assert [ups_mod.LEVEL[s] for s in ("online", "charging", "battery", "low_battery", "replace", "overload", "unknown")] == \
        ["ok", "ok", "warn", "danger", "warn", "warn", "unknown"]


# ---------------------------------------------------------------------------------------------------- the book
def reading(status="OL", name="office", **kw):
    d = {"name": name, "description": "Rack UPS", "status": status, "charge": 100.0, "charge_low": 20.0, "runtime_s": 1800.0, "load": 30.0,
         "input_v": 230.0, "output_v": 230.0, "battery_v": 27.0, "temp": 28.0, "power_w": 200.0, "test": "passed", "model": "UPS-1"}
    d.update(kw)
    return d


def book():
    core = FakeCore()
    return core, UpsBook(core, clock=lambda: T0)


def log(b):
    return [(r["t"], r["state"]) for r in b.store.query("SELECT t, state FROM ups_log ORDER BY t, rowid")]


def test_a_change_of_state_is_logged_and_the_same_state_is_not():
    core, b = book()
    b.feed("nut", 1, "192.168.50.10", [reading("OL")], T0)
    b.feed("nut", 1, "192.168.50.10", [reading("OL")], T0 + 10)
    b.feed("nut", 1, "192.168.50.10", [reading("OB", charge=95.0)], T0 + 20)
    b.feed("nut", 1, "192.168.50.10", [reading("OB", charge=90.0)], T0 + 30)
    b.feed("nut", 1, "192.168.50.10", [reading("OL CHRG", charge=90.0)], T0 + 40)
    assert log(b) == [(T0, "online"), (T0 + 20, "battery"), (T0 + 40, "charging")]
    assert b.units["nut:1:office"]["since"] == T0 + 40


def test_since_stays_while_the_state_stays():
    core, b = book()
    b.feed("nut", 1, "h", [reading("OL")], T0)
    b.feed("nut", 1, "h", [reading("OL", charge=99.0)], T0 + 100)
    assert b.view(T0 + 100)[0]["since_t"] == T0


def test_junk_readings_are_skipped_and_two_units_stay_apart():
    core, b = book()
    b.feed("nut", 1, "h", ["x", None, 5, reading("OL", name="a"), reading("OB", name="b"), {"status": "OL"}], T0)
    assert sorted(b.units) == ["nut:1:a", "nut:1:b", "nut:1:ups"]
    b.feed("snmp", 2, "h2", [reading("OL", name="a")], T0)
    assert "snmp:2:a" in b.units


def test_stale_reading_is_unknown_never_ok():
    core, b = book()
    b.feed("nut", 1, "h", [reading("OL")], T0)
    assert b.view(T0 + 100)[0]["state"] == "online" and b.view(T0 + 100)[0]["fresh"] == "fresh"
    v = b.view(T0 + 400)[0]
    assert v["state"] == "online" and v["fresh"] == "late" and v["level"] == "ok"
    v = b.view(T0 + 901)[0]
    assert v["state"] == "unknown" and v["level"] == "unknown" and v["fresh"] == "stale" and v["since_t"] is None
    assert b.view(T0 + 900)[0]["state"] == "online"                        # exactly 15 minutes is still a reading


def test_a_stale_unit_on_battery_is_unknown_too():
    core, b = book()
    b.feed("nut", 1, "h", [reading("OB LB", charge=10.0)], T0)
    assert b.view(T0 + 1000)[0]["level"] == "unknown"


def test_view_fields_and_runtime_hints():
    core, b = book()
    core.ip_to_device = {"192.168.50.10": "d-test-1"}
    b.feed("nut", 1, "192.168.50.10", [reading("OL")], T0)
    v = b.view(T0 + 5)[0]
    assert v["name"] == "Rack UPS" and v["ups"] == "office" and v["device"] == "d-test-1" and v["model"] == "UPS-1"
    assert v["charge"] == 100.0 and v["runtime_s"] == 1800.0 and v["runtime_if_failed"] is True and v["runtime_low"] is False
    assert v["load"] == 30.0 and v["power_w"] == 200.0 and v["test"] == "passed" and v["source"] == "nut" and v["age_s"] == 5.0
    b.feed("nut", 1, "192.168.50.10", [reading("OB", runtime_s=500.0, charge=80.0)], T0 + 10)
    v = b.view(T0 + 10)[0]
    assert v["state"] == "battery" and v["runtime_if_failed"] is False and v["runtime_low"] is True and v["level"] == "warn"
    b.feed("nut", 1, "192.168.50.10", [reading("OB", runtime_s=1500.0, charge=80.0)], T0 + 20)
    assert b.view(T0 + 20)[0]["runtime_low"] is False


def test_view_hides_junk_numbers():
    core, b = book()
    b.feed("nut", 1, "h", [reading("OL", charge="n/a", runtime_s=float("nan"), load=None)], T0)
    v = b.view(T0)[0]
    assert v["charge"] is None and v["runtime_s"] is None and v["load"] is None


def test_view_orders_worst_first_then_by_name():
    core, b = book()
    rows = [reading(st, name=n, description=n.upper()) for st, n in (("OL", "zeta"), ("OL", "alpha"), ("OB", "mid"), ("OB LB", "low"), ("", "silent"), ("RB", "old"))]
    b.feed("nut", 1, "h", rows, T0)
    assert [r["ups"] for r in b.view(T0)] == ["low", "mid", "old", "silent", "alpha", "zeta"]


def test_runtime_for_prefers_the_ups_of_the_device_and_skips_stale_ones():
    core, b = book()
    core.ip_to_device = {"h1": "d1", "h2": "d2"}
    b.feed("nut", 1, "h1", [reading("OL", name="one")], T0)
    b.feed("nut", 2, "h2", [reading("OB", name="two")], T0)
    assert b.runtime_for("d1", T0)["ups"] == "one"
    assert b.runtime_for("d9", T0)["ups"] == "two"                           # none for that device: the worst one
    assert b.runtime_for(None, T0)["ups"] == "two"
    assert b.runtime_for("d1", T0 + 2000) is None
    assert UpsBook(FakeCore(), clock=lambda: T0).runtime_for("d1") is None


def test_forget_drops_the_units_of_a_connection():
    core, b = book()
    b.feed("nut", 1, "h", [reading("OL", name="a")], T0)
    b.feed("nut", 2, "h", [reading("OL", name="a")], T0)
    b.forget(1)
    assert list(b.units) == ["nut:2:a"]


def test_log_keeps_35_days():
    core, b = book()
    b.feed("nut", 1, "h", [reading("OL")], T0 - 40 * 86400)
    b.feed("nut", 1, "h", [reading("OB")], T0)
    assert [s for _t, s in log(b)] == ["battery"]


# ---------------------------------------------------------------------------------------------------- history
def put(b, rows, unit="nut:1:office"):
    for t, st in rows:
        b.store.execute("INSERT INTO ups_log(t, unit, state, charge, runtime_s) VALUES (?,?,?,?,?)", (t, unit, st, None, None))


def test_episodes_closed_open_and_low_battery():
    core, b = book()
    put(b, [(100, "online"), (200, "battery"), (260, "online"), (400, "battery"), (430, "low_battery"), (470, "charging"), (600, "battery")])
    assert b.episodes(0, 700) == [
        {"unit": "nut:1:office", "from_t": 200, "to_t": 260, "state": "battery", "open": False},
        {"unit": "nut:1:office", "from_t": 400, "to_t": 470, "state": "low_battery", "open": False},
        {"unit": "nut:1:office", "from_t": 600, "to_t": 700, "state": "battery", "open": True}]


def test_episodes_window_cuts_and_skips():
    core, b = book()
    put(b, [(100, "battery"), (300, "online"), (500, "battery"), (800, "online")])
    assert b.episodes(150, 250) == [{"unit": "nut:1:office", "from_t": 150, "to_t": 250, "state": "battery", "open": True}]
    assert b.episodes(350, 450) == []                                         # between two episodes
    assert [(e["from_t"], e["to_t"]) for e in b.episodes(350, 600)] == [(500, 600)]
    assert [(e["from_t"], e["to_t"]) for e in b.episodes(250, 900)] == [(250, 300), (500, 800)]
    assert b.episodes(900, 1000) == []


def test_episodes_two_units_sorted_by_start():
    core, b = book()
    put(b, [(300, "battery"), (400, "online")], "nut:1:b")
    put(b, [(100, "battery"), (200, "online")], "nut:1:a")
    assert [e["unit"] for e in b.episodes(0, 500)] == ["nut:1:a", "nut:1:b"]


def test_unknown_in_between_ends_the_episode_it_is_no_battery_claim():
    core, b = book()
    put(b, [(100, "battery"), (200, "unknown")])
    eps = b.episodes(0, 500)
    assert eps == [{"unit": "nut:1:office", "from_t": 100, "to_t": 200, "state": "battery", "open": False}]


# ---------------------------------------------------------------------------------------------------- UPS-MIB (RFC 1628)
B = snmp.OID_UPS


def mib(**cols):
    return [("%s.%s" % (B, k.replace("_", ".")), v) for k, v in cols.items()]


def test_ups_mib_on_mains():
    rows = mib(**{"1_2_0": "UPS-1500", "1_5_0": "office", "2_1_0": 2, "2_3_0": 38, "2_4_0": 100, "2_5_0": 273, "2_7_0": 29, "3_3_1_3_1": 231,
                  "4_1_0": 3, "4_4_1_2_1": 230, "4_4_1_4_1": 210, "4_4_1_5_1": 31})
    r = snmp.parse_ups_mib(rows)
    assert r["name"] == "office" and r["description"] == "UPS-1500" and r["status"] == "OL" and r["model"] == "UPS-1500"
    assert r["charge"] == 100 and r["runtime_s"] == 38 * 60 and r["battery_v"] == 27.3 and r["temp"] == 29
    assert r["input_v"] == 231 and r["output_v"] == 230 and r["power_w"] == 210 and r["load"] == 31
    assert state_of(r["status"]) == "online"


def test_ups_mib_on_battery_and_low_battery():
    r = snmp.parse_ups_mib(mib(**{"2_1_0": 2, "4_1_0": 5, "2_4_0": 80, "2_3_0": 12}))
    assert r["status"] == "OB" and state_of(r["status"], r["charge"], r["runtime_s"]) == "battery"
    r = snmp.parse_ups_mib(mib(**{"2_1_0": 3, "4_1_0": 5}))
    assert r["status"] == "OB LB" and state_of(r["status"]) == "low_battery"
    r = snmp.parse_ups_mib(mib(**{"2_1_0": 4, "4_1_0": 3}))
    assert r["status"] == "OL LB"


@pytest.mark.parametrize("src,word", [(1, ""), (2, ""), (3, "OL"), (4, "OL"), (5, "OB"), (6, "OL"), (7, "OL"), (99, "")])
def test_ups_mib_output_source_table(src, word):
    assert snmp.parse_ups_mib(mib(**{"4_1_0": src}))["status"] == word


def test_ups_mib_nothing_to_say_is_an_empty_reading():
    assert snmp.parse_ups_mib([]) == {}
    assert snmp.parse_ups_mib([("1.3.6.1.2.1.1.1.0", "router")]) == {}                    # rows outside the UPS tree
    r = snmp.parse_ups_mib(mib(**{"1_5_0": "x"}))
    assert r["status"] == "" and state_of(r["status"]) == "unknown"


def test_ups_mib_junk_values():
    r = snmp.parse_ups_mib(mib(**{"2_4_0": 250, "2_3_0": -1, "2_5_0": "n/a", "2_7_0": True, "4_1_0": 3, "1_5_0": "n" * 200}))
    assert r["charge"] is None and r["runtime_s"] is None and r["battery_v"] is None and r["temp"] is None
    assert len(r["name"]) == 64 and r["status"] == "OL"
    r = snmp.parse_ups_mib(mib(**{"2_3_0": 0, "2_4_0": 0, "4_1_0": 5}))
    assert r["charge"] == 0 and r["runtime_s"] == 0.0                                      # zero is a value, not "nothing"


def test_ups_mib_first_row_wins_and_name_falls_back_to_the_model():
    rows = mib(**{"1_2_0": "Model X", "4_4_1_5_1": 40}) + mib(**{"1_2_0": "Other", "4_4_1_5_1": 90})
    r = snmp.parse_ups_mib(rows)
    assert r["name"] == "Model X" and r["load"] == 40
    assert snmp.parse_ups_mib(mib(**{"4_1_0": 3}))["name"] == "ups"
