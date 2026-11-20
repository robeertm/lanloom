"""Energy flow (lanloom/energy.py): classification of power sensors, signs and flows, consumer lanes, settings, counting per day,
the control room block, the nightly section, the sample data and the routes."""
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crhelp import T0, FakeCore, ent, hub_entry               # noqa: E402
from lanloom import access, auth, demo_energy                # noqa: E402
from lanloom import config as config_mod                     # noqa: E402
from lanloom.controlroom.room import ControlRoom             # noqa: E402
from lanloom.energy import EnergyBook, price_unit, role_of   # noqa: E402
from lanloom.main import create_app                          # noqa: E402

HDR = {"X-Lanloom": "1"}


def book_of(rows, states=None, devices=None, t=T0):
    """rows: [(entity_id, name, unit, value, device?)] -> a book over a fake core with one hub."""
    core = FakeCore()
    ents, st = [], {}
    for r in rows:
        eid, name, unit, value = r[:4]
        e = ent(eid, r[4] if len(r) > 4 else None, name, unit, "power" if unit in ("W", "kW", "MW") else None)
        e["area"] = None
        ents.append(e)
        st[eid] = (str(value), t)
    st.update(states or {})
    core.hub = {1: hub_entry(ents, st, devices, t=t)}
    return core, EnergyBook(core, clock=lambda: T0)


def flows(v):
    return {f["id"]: f["w"] for f in v["flows"]}


# ---------------------------------------------------------------------------------------------------- availability and classification
def test_not_available_without_power_sensors():
    core, b = book_of([("sensor.temp", "Kitchen temperature", "°C", 21)])
    v = b.view(T0)
    assert v["available"] is False and v["flows"] == [] and v["lanes"] == [] and v["fresh"]["level"] == "none"
    assert EnergyBook(FakeCore(), clock=lambda: T0).view(T0)["available"] is False


def test_roles_from_names_and_kilowatt_conversion():
    assert role_of("grid power") == "grid" and role_of("mains import") == "grid" and role_of("netz bezug") == "grid"
    assert role_of("grid export power") == "export" and role_of("einspeisung") == "export"
    assert role_of("solar power") == "solar" and role_of("pv leistung") == "solar" and role_of("hybrid inverter") == "solar"
    assert role_of("home battery power") == "battery" and role_of("akku leistung") == "battery"
    assert role_of("heat pump power") is None and role_of("pvc pipe heater") is None
    core, b = book_of([("sensor.g", "Grid power", "kW", 1.5), ("sensor.s", "Solar power", "W", 0)])
    v = b.view(T0)
    assert v["now"]["grid_w"] == 1500.0 and v["now"]["solar_w"] == 0.0 and v["now"]["home_w"] == 1500.0


def test_price_and_co2_entities_with_units():
    assert price_unit("EUR/kWh") == (1.0, "EUR") and price_unit("€/kWh") == (1.0, "EUR") and price_unit("ct/kWh") == (0.01, None)
    assert price_unit("$/kWh") == (1.0, "USD") and price_unit("W") is None
    core, b = book_of([("sensor.g", "Grid power", "W", 100), ("sensor.p", "Spot price", "ct/kWh", 28.5),
                       ("sensor.c", "Grid carbon intensity", "gCO2eq/kWh", 210)])
    v = b.view(T0)
    assert v["price"] == {"value": 0.285, "currency": "EUR", "source": "entity"} and v["co2"] == {"g_kwh": 210.0, "source": "entity"}
    core, b = book_of([("sensor.g", "Grid power", "W", 100), ("sensor.p", "Tariff", "GBP/kWh", 0.3)])
    assert b.view(T0)["price"]["currency"] == "GBP"
    core, b = book_of([("sensor.g", "Grid power", "W", 100)])
    v = b.view(T0)
    assert v["price"] is None and v["co2"] == {"g_kwh": 400.0, "source": "default"}


# ---------------------------------------------------------------------------------------------------- signs and flows
def test_flows_add_up_for_surplus_with_battery_and_export():
    f = EnergyBook.flows(home=1000, solar=4000, battery=-2000, grid=-1000)
    d = {x["id"]: x["w"] for x in f}
    assert d == {"solar-home": 1000.0, "solar-battery": 2000.0, "solar-grid": 1000.0}
    assert sum(w for i, w in d.items() if i.endswith("-home")) == 1000.0


def test_night_import_and_battery_discharge():
    assert {x["id"]: x["w"] for x in EnergyBook.flows(800, 0, 0, 800)} == {"grid-home": 800.0}
    d = {x["id"]: x["w"] for x in EnergyBook.flows(1000, 0, 600, 400)}
    assert d == {"battery-home": 600.0, "grid-home": 400.0}
    d = {x["id"]: x["w"] for x in EnergyBook.flows(500, 0, -1500, 2000)}                  # charging from the grid at night
    assert d == {"grid-home": 500.0, "grid-battery": 1500.0}


def test_view_derives_home_from_grid_solar_battery():
    core, b = book_of([("sensor.g", "Grid power", "W", -1000), ("sensor.s", "Solar power", "W", 4000),
                       ("sensor.b", "Home battery power", "W", -2000)])
    v = b.view(T0)
    assert v["now"] == {"grid_w": -1000.0, "solar_w": 4000.0, "battery_w": -2000.0, "home_w": 1000.0}
    assert flows(v) == {"solar-home": 1000.0, "solar-battery": 2000.0, "solar-grid": 1000.0}


def test_separate_import_and_export_sensors():
    core, b = book_of([("sensor.gi", "Grid import power", "W", 0), ("sensor.ge", "Grid export power", "W", 700),
                       ("sensor.s", "Solar power", "W", 1700)])
    v = b.view(T0)
    assert v["now"]["grid_w"] == -700.0 and v["now"]["home_w"] == 1000.0
    assert flows(v) == {"solar-home": 1000.0, "solar-grid": 700.0}


def test_total_sensor_wins_over_its_parts_and_phases_add():
    core, b = book_of([("sensor.s1", "Solar string 1 power", "W", 100), ("sensor.s2", "Solar string 2 power", "W", 200),
                       ("sensor.st", "Solar total power", "W", 300), ("sensor.l1", "Grid power L1", "W", 100), ("sensor.l2", "Grid power L2", "W", 50)])
    v = b.view(T0)
    assert v["now"]["solar_w"] == 300.0 and v["now"]["grid_w"] == 150.0


# ---------------------------------------------------------------------------------------------------- lanes
def test_lanes_with_cost_co2_area_and_network_device():
    core, b = book_of([("sensor.g", "Grid power", "W", 1500), ("sensor.hp", "Heat pump power", "W", 900, "dev-hp"),
                       ("sensor.tv", "TV power", "W", 100, "dev-tv"), ("sensor.off", "Idle plug power", "W", 0),
                       ("sensor.p", "Price", "EUR/kWh", 0.30), ("sensor.neg", "Odd power", "W", -20)],
                      devices=[{"id": "dev-hp", "name": "Heat pump"}, {"id": "dev-tv", "name": "TV"}])
    core.hub_by = {"1:dev-hp": "net-1"}
    core.hub[1]["snapshot"]["areas"] = [{"id": "garage", "name": "Garage"}]
    core.hub[1]["snapshot"]["entities"][1]["area"] = "garage"
    v = b.view(T0)
    assert [x["id"] for x in v["lanes"]] == ["sensor.hp", "sensor.tv"]                   # idle and negative ones are not lanes
    hp, tv = v["lanes"]
    assert hp["w"] == 900.0 and hp["cost_h"] == 0.27 and hp["co2_h_g"] == 360.0 and hp["area"] == "Garage"
    assert hp["net_id"] == "net-1" and hp["device"] == "dev-hp" and tv["net_id"] is None and tv["area"] is None
    assert hp["share"] == 0.6 and v["rest_w"] == 500.0 and v["grid_share"] == 1.0


def test_a_lane_costs_only_the_share_the_grid_supplies():
    # 1000 W in the home: 750 W from the roof, 250 W from the grid -> a quarter of the price and the CO2
    core, b = book_of([("sensor.g", "Grid power", "W", 250), ("sensor.pv", "Solar power", "W", 750), ("sensor.hp", "Heat pump power", "W", 800, "dev-hp"),
                       ("sensor.p", "Price", "EUR/kWh", 0.40)], devices=[{"id": "dev-hp", "name": "Heat pump"}])
    v = b.view(T0)
    hp = v["lanes"][0]
    assert v["grid_share"] == 0.25 and hp["cost_h"] == 0.08 and hp["co2_h_g"] == 80.0
    # the roof covers everything and feeds in: nothing costs anything
    core2, b2 = book_of([("sensor.g", "Grid power", "W", -500), ("sensor.pv", "Solar power", "W", 1500), ("sensor.hp", "Heat pump power", "W", 800, "dev-hp"),
                         ("sensor.p", "Price", "EUR/kWh", 0.40)], devices=[{"id": "dev-hp", "name": "Heat pump"}])
    v2 = b2.view(T0)
    assert v2["grid_share"] == 0.0 and v2["lanes"][0]["cost_h"] == 0.0 and v2["lanes"][0]["co2_h_g"] == 0.0


def test_rest_never_below_zero_and_lanes_capped():
    rows = [("sensor.g", "Grid power", "W", 100)] + [("sensor.c%d" % i, "Consumer %d power" % i, "W", 10 + i) for i in range(30)]
    core, b = book_of(rows)
    v = b.view(T0)
    assert len(v["lanes"]) == 24 and v["lanes"][0]["w"] >= v["lanes"][-1]["w"] and v["rest_w"] == 0.0


def test_without_grid_the_home_is_the_sum_of_the_lanes():
    core, b = book_of([("sensor.a", "Heat pump power", "W", 600), ("sensor.b", "Fridge power", "W", 100)])
    v = b.view(T0)
    assert v["now"]["grid_w"] is None and v["now"]["home_w"] == 700.0 and v["rest_w"] == 0.0
    assert flows(v) == {"grid-home": 700.0}


def test_freshness_of_the_readings():
    core, b = book_of([("sensor.g", "Grid power", "W", 100)], t=T0 - 30)
    assert b.view(T0)["fresh"] == {"age_s": 30.0, "level": "fresh"}
    core, b = book_of([("sensor.g", "Grid power", "W", 100)], t=T0 - 2000)
    assert b.view(T0)["fresh"]["level"] == "stale"


# ---------------------------------------------------------------------------------------------------- settings
def test_settings_validation():
    core, b = book_of([("sensor.g", "Grid power", "W", 100), ("sensor.p", "Spot price", "EUR/kWh", 0.2)])
    cur = b.set_settings({"price": 0.35, "feed_in": "0.08", "co2_g_kwh": 250, "currency": "CHF"})
    assert (cur["price"], cur["feed_in"], cur["co2_g_kwh"], cur["currency"]) == (0.35, 0.08, 250.0, "CHF")
    assert core.store.get_setting("energy")["price"] == 0.35
    for bad, key in (({"price": -1}, "price"), ({"price": 11}, "price"), ({"price": "x"}, "price"), ({"price": True}, "price"),
                     ({"feed_in": 99}, "feed_in"), ({"co2_g_kwh": 2001}, "co2"), ({"currency": "eur"}, "currency"),
                     ({"currency": "EURO"}, "currency"), ({"price_entity": "sensor.g"}, "entity"), ({"co2_entity": "sensor.nope"}, "entity"),
                     ("text", "body")):
        with pytest.raises(ValueError, match="energy.err." + key):
            b.set_settings(bad)
    assert b.set_settings({"price": None})["price"] is None
    assert b.set_settings({"price_entity": "sensor.p"})["price_entity"] == "sensor.p"
    assert b.set_settings({"price_entity": ""})["price_entity"] is None


def test_price_precedence_entity_choice_beats_number_beats_auto_entity():
    core, b = book_of([("sensor.g", "Grid power", "W", 100), ("sensor.p", "Spot price", "EUR/kWh", 0.2)])
    assert b.view(T0)["price"]["source"] == "entity"                                      # found by itself
    b.set_settings({"price": 0.5})
    assert b.view(T0)["price"] == {"value": 0.5, "currency": "EUR", "source": "setting"}   # a typed number beats it
    b.set_settings({"price_entity": "sensor.p"})
    assert b.view(T0)["price"] == {"value": 0.2, "currency": "EUR", "source": "entity"}    # a named entity beats the number
    core.hub[1]["states"]["sensor.p"] = ("unavailable", T0)
    assert b.view(T0)["price"]["source"] == "setting"                                      # a dead entity falls back to the number
    b.set_settings({"co2_g_kwh": 120})
    assert b.view(T0)["co2"] == {"g_kwh": 120.0, "source": "setting"}


# ---------------------------------------------------------------------------------------------------- counting
def test_step_counts_trapezoid_per_day_and_skips_gaps():
    core, b = book_of([("sensor.g", "Grid power", "W", 1000), ("sensor.s", "Solar power", "W", 0)])
    b.set_settings({"price": 0.30, "co2_g_kwh": 400, "feed_in": 0.1})
    t0 = datetime(2026, 11, 10, 12, 0, 0).timestamp()
    for i in range(7):                                                                     # an hour in ten-minute samples
        b.step(t0 + i * 600)
    day = b.day_key(t0)
    t = b.day_totals(day)
    assert t["kwh"] == pytest.approx(1.0) and t["cost"] == pytest.approx(0.30) and t["co2_kg"] == pytest.approx(0.4) and t["solar_kwh"] == 0.0
    core.hub[1]["states"]["sensor.g"] = ("-500", t0)                                       # exporting now
    core.hub[1]["states"]["sensor.s"] = ("2500", t0)                                       # home 2000 W
    b.step(t0 + 7200)                                                                      # a 1 h gap > 10 min: not counted
    assert b.day_totals(day)["kwh"] == pytest.approx(1.0)
    for i in range(1, 4):                                                                  # half an hour at 2 kW, 0.5 kW exported
        b.step(t0 + 7200 + i * 600)
    t = b.day_totals(day)
    assert t["kwh"] == pytest.approx(2.0) and t["solar_kwh"] == pytest.approx(1.25) and t["export_kwh"] == pytest.approx(0.25)
    assert t["cost"] == pytest.approx(0.30 - 0.25 * 0.1)
    v = b.view(t0 + 7200 + 1800)
    assert v["today"]["source"] == "counted" and v["today"]["kwh"] == 2.0 and v["today"]["solar_kwh"] == 1.25


def test_step_persists_at_most_every_five_minutes_and_at_day_change_and_keeps_35_days():
    core, b = book_of([("sensor.g", "Grid power", "W", 1000)])
    t0 = datetime(2026, 11, 10, 23, 50, 0).timestamp()
    b.step(t0)
    b.step(t0 + 60)
    assert core.store.get_setting("energy_days", None) is not None                         # first save
    first = core.store.get_setting("energy_days")
    b.step(t0 + 120)
    assert core.store.get_setting("energy_days") == first                                  # within 5 minutes: not written again
    b.step(t0 + 900)                                                                       # over midnight, gap too long to count, day changed -> saved
    assert b.day_key(t0 + 900) != b.day_key(t0)
    for i in range(40):
        b._days["2026-01-%02d" % (i % 28 + 1) if i < 28 else "2025-12-%02d" % (i - 27)] = {"kwh": 1.0}
    b._save(t0 + 5000, force=True)
    assert len(core.store.get_setting("energy_days")) <= 35
    b2 = EnergyBook(core, clock=lambda: T0)
    assert b2.day_totals(b.day_key(t0)) is not None                                        # a restart keeps the totals


def test_today_counter_entity_wins_and_is_recorded():
    core, b = book_of([("sensor.g", "Grid power", "W", 1000), ("sensor.s", "Solar power", "W", 2000), ("sensor.p", "Price", "EUR/kWh", 0.25)],
                      states={})
    core.hub[1]["snapshot"]["entities"] += [ent("sensor.e", None, "Energy today", "kWh"), ent("sensor.se", None, "Solar energy today", "kWh"),
                                            ent("sensor.xe", None, "Grid export today", "kWh"), ent("sensor.life", None, "Energy total", "kWh")]
    core.hub[1]["states"].update({"sensor.e": ("10", T0), "sensor.se": ("8", T0), "sensor.xe": ("3", T0), "sensor.life": ("999", T0)})
    v = b.view(T0)
    td = v["today"]
    assert td["source"] == "entity" and td["kwh"] == 10.0 and td["solar_kwh"] == 8.0 and td["export_kwh"] == 3.0
    assert td["cost"] == pytest.approx((10 - 5) * 0.25, abs=0.01) and td["co2_kg"] == pytest.approx(5 * 0.4, abs=0.01)
    b.step(T0)
    assert b.day_totals(b.day_key(T0))["kwh"] == 10.0                                      # the last value of the day stays


# ---------------------------------------------------------------------------------------------------- control room block
def test_control_room_block_only_with_energy_and_stale_is_unknown():
    core, b = book_of([("sensor.g", "Grid power", "W", 1200), ("sensor.s", "Solar power", "W", 500), ("sensor.p", "Price", "EUR/kWh", 0.3)])
    core.energy = b
    cr = ControlRoom(core, clock=lambda: T0)
    blk = next((x for x in cr.snapshot(T0)["blocks"] if x["id"] == "energy"), None)
    assert blk and blk["level"] == "ok" and blk["link"] == "#/energy"
    keys = [ln["key"] for ln in blk["lines"]]
    assert keys[:2] == ["cr.en.now", "cr.en.solar"] and blk["lines"][0]["vars"] == {"w": 1700}
    blk = next(x for x in cr.snapshot(T0 + 5000)["blocks"] if x["id"] == "energy")
    assert blk["level"] == "unknown" and blk["fresh"] == "stale"
    core2, b2 = book_of([("sensor.t", "Temp", "°C", 20)])
    core2.energy = b2
    assert all(x["id"] != "energy" for x in ControlRoom(core2, clock=lambda: T0).snapshot(T0)["blocks"])


# ---------------------------------------------------------------------------------------------------- nightly section
def test_night_section_with_yesterday_total(tmp_path):
    from test_controlroom_report import NOW, make, build            # noqa: F401
    from lanloom import energy_report                               # noqa: F401
    c, rep = make(tmp_path)
    r, sec = (lambda r: (r, next((s for s in r["sections"] if s["id"] == "energy"), None)))(rep.assemble(NOW - 86400, NOW, None, "2026-11-16", []))
    assert sec is None
    day = c.energy.yesterday_key(NOW)
    assert day == "2026-11-15"
    c.energy._days = {day: {"kwh": 14.0, "cost": 2.5, "co2_kg": 3.1, "solar_kwh": 9.0, "export_kwh": 2.0, "since": None}}
    r = rep.assemble(NOW - 86400, NOW, None, "2026-11-16", [])
    sec = next(s for s in r["sections"] if s["id"] == "energy")
    ids = [i["id"] for i in sec["items"]]
    assert ids == ["energy-kwh", "energy-cost", "energy-co2", "energy-solar"] and sec["summary"]["vars"]["kwh"] == 14.0
    assert next(i for i in sec["items"] if i["id"] == "energy-solar")["vars"]["pct"] == 50      # (9 - 2) / 14
    assert not any(i["point"] for i in sec["items"])


# ---------------------------------------------------------------------------------------------------- sample data
def test_demo_model_adds_up_and_follows_the_sun():
    night = datetime(2026, 11, 10, 2, 0).timestamp()
    noon = datetime(2026, 11, 10, 12, 30).timestamp()
    for ts in (night, noon):
        m = demo_energy.model(ts)
        assert m["grid"] == pytest.approx(m["home"] - m["sensor.solar_power"] - m["sensor.home_battery_power"])
    assert demo_energy.model(night)["sensor.solar_power"] == 0.0 and demo_energy.model(night)["grid"] > 0
    assert 3000 < demo_energy.model(noon)["sensor.solar_power"] <= 4300
    frames = demo_energy.tick(noon, random.Random(1))
    assert {f["entity_id"] for f in frames} >= {"sensor.solar_power", "sensor.home_battery_power", "sensor.wlan_meter_1"}
    assert all(f["type"] == "hub_state" for f in frames)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("energy")
    mp = pytest.MonkeyPatch()
    mp.setenv("LANLOOM_DATA", str(tmp))
    mp.setenv("LANLOOM_DEMO", "1")
    mp.setenv("LANLOOM_DEMO_SEED", "1")
    mp.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        c.st = st
        c.uids = {role: st.accounts.create("user-" + role, "a-long-test-password", role=role) for role in access.ROLES}
        deadline = time.time() + 30
        while time.time() < deadline:
            c.cookies.clear()
            c.cookies.set(auth.SESSION_COOKIE, st.sessions.create(c.uids["viewer"], ip="127.0.0.1", agent="test"))
            if c.get("/api/energy").json().get("available"):
                break
            time.sleep(0.3)
        c.cookies.clear()
        yield c
    mp.undo()


def call(c, method, path, body=None, role=None):
    c.cookies.clear()
    if role:
        c.cookies.set(auth.SESSION_COOKIE, c.st.sessions.create(c.uids[role], ip="127.0.0.1", agent="test"))
    try:
        return c.request(method, path, headers=HDR, json=body if body is not None else {})
    finally:
        c.cookies.clear()


def test_rules_roles():
    assert access.RULES[("GET", "/api/energy")].role == "viewer"
    rule = access.RULES[("PUT", "/api/energy/settings")]
    assert rule.role == "operator" and rule.audit == "energy.settings"


def test_demo_snapshot_has_energy(client):
    v = call(client, "GET", "/api/energy", role="viewer").json()
    assert v["available"] is True and v["now"]["home_w"] and v["lanes"] and v["price"]["value"] == 0.31 and v["co2"]["source"] == "entity"
    names = {x["name"] for x in v["lanes"]}
    assert {"Heat pump power", "Server rack power"} <= names or len(names) >= 3
    assert v["today"]["kwh"] is not None
    blk = [b for b in call(client, "GET", "/api/controlroom", role="viewer").json()["blocks"] if b["id"] == "energy"]
    assert blk
    assert any(client.st.core.energy.day_totals(d) for d in client.st.core.energy.days())


def test_routes_rights_and_audit(client):
    assert call(client, "GET", "/api/energy").status_code == 401
    assert call(client, "PUT", "/api/energy/settings", {"price": 0.4}).status_code == 401
    r = call(client, "PUT", "/api/energy/settings", {"price": 0.4}, role="viewer")
    assert r.status_code == 403 and r.json()["error"] == "auth.err.role"
    r = call(client, "PUT", "/api/energy/settings", {"price": 0.4, "currency": "USD"}, role="operator")
    assert r.status_code == 200 and r.json()["price"] == 0.4
    v = call(client, "GET", "/api/energy", role="viewer").json()
    assert v["settings"]["price"] == 0.4 and v["price"] == {"value": 0.4, "currency": "USD", "source": "setting"}
    r = call(client, "PUT", "/api/energy/settings", {"price": 99}, role="operator")
    assert r.status_code == 400 and r.json()["error"] == "energy.err.price"
    r = call(client, "PUT", "/api/energy/settings", {"price_entity": "sensor.solar_power"}, role="admin")
    assert r.status_code == 400 and r.json()["error"] == "energy.err.entity"
    call(client, "PUT", "/api/energy/settings", {"price": None, "currency": "EUR"}, role="admin")
