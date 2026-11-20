"""Sample data of the energy view (S17): a solar roof, a home battery, a price and a CO2 figure, and consumers on the sample hub.

The model is a function of the local time of day (solar follows a daylight curve and is zero at night, a heat pump breathes, a desk
draws power in office hours), so the picture is plausible whenever the sample network is opened. The grid reading is always
``home - solar - battery``, so the flows add up. Fictional names only; nothing is contacted. A few past days are seeded once so that
"today" and the nightly section have data."""
from __future__ import annotations

import math
import os
import random
import time
from datetime import datetime, timedelta

PRICE = 0.31
CO2 = 320.0
REST_W = 150.0                       # what no sensor measures
WASHER_W = 412.5
# new hub devices: (id, name, area)
DEVICES = [("energy-inverter", "Solar inverter", "garage"), ("energy-battery", "Home battery", "garage"),
           ("energy-heatpump", "Heat pump", "garage"), ("energy-rack", "Server rack", "office"), ("energy-fridge", "Fridge", "kitchen"),
           ("energy-tv", "Living room TV", "living_room"), ("energy-desk", "Office desk", "office")]
# new sensors: (entity id, name, device id or None, area, unit, device class)
SENSORS = [("sensor.solar_power", "Solar power", "energy-inverter", "garage", "W", "power"),
           ("sensor.home_battery_power", "Home battery power", "energy-battery", "garage", "W", "power"),
           ("sensor.heat_pump_power", "Heat pump power", "energy-heatpump", "garage", "W", "power"),
           ("sensor.server_rack_power", "Server rack power", "energy-rack", "office", "W", "power"),
           ("sensor.fridge_power", "Fridge power", "energy-fridge", "kitchen", "W", "power"),
           ("sensor.living_room_tv_power", "Living room TV power", "energy-tv", "living_room", "W", "power"),
           ("sensor.office_desk_power", "Office desk power", "energy-desk", "office", "W", "power"),
           ("sensor.solar_energy_today", "Solar energy today", "energy-inverter", "garage", "kWh", "energy"),
           ("sensor.electricity_price", "Electricity price", None, None, "EUR/kWh", "monetary"),
           ("sensor.grid_carbon_intensity", "Grid carbon intensity", None, None, "gCO2eq/kWh", None)]
EXISTING = {"washer": "Washer power", "grid": "Grid power", "today": "Energy today"}


def _hour(now: float) -> float:
    fixed = os.environ.get("LANLOOM_DEMO_HOUR")         # screenshots: the sample house at a fixed hour of the day (e.g. 12.5)
    if fixed:
        try:
            v = float(fixed)
            if 0.0 <= v < 24.0:
                return v
        except ValueError:
            pass
    lt = time.localtime(now)
    return lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0


def model(now: float) -> dict:
    """{entity id or key: value} without noise: watts for power sensors, kWh for counters."""
    h = _hour(now)
    cloud = 1.0 - 0.08 * (0.5 + 0.5 * math.sin(now / 700.0))
    solar = max(0.0, math.sin(math.pi * (h - 6.0) / 14.0)) ** 1.3 * 4200.0 * cloud if 6.0 < h < 20.0 else 0.0
    heat = 120.0 + 520.0 * (0.5 + 0.5 * math.sin(now / 1500.0)) if 5.0 <= h < 23.0 else 90.0
    lanes = {"sensor.heat_pump_power": heat,
             "sensor.server_rack_power": 120.0 + 10.0 * math.sin(now / 97.0),
             "sensor.fridge_power": 12.0 if int(now // 600) % 3 == 0 else 85.0,
             "sensor.living_room_tv_power": 105.0 if 18.0 <= h < 23.0 else 2.0,
             "sensor.office_desk_power": 140.0 if 8.0 <= h < 18.0 else 6.0,
             "washer": WASHER_W if 9.0 <= h < 11.0 else 2.0}
    home = sum(lanes.values()) + REST_W
    surplus = solar - home
    battery = -min(2500.0, surplus) if surplus > 0 else min(1800.0, -surplus * 0.7)
    out = dict(lanes, home=home, **{"sensor.solar_power": solar, "sensor.home_battery_power": battery, "grid": home - solar - battery})
    return out


def _today_kwh(now: float) -> tuple:
    """(home kWh, solar kWh) since local midnight, integrated over the model in 5 minute steps."""
    lt = time.localtime(now)
    start = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    home = solar = 0.0
    t = start
    while t < now:
        m = model(t + 150.0)
        dt = min(300.0, now - t)
        home += m["home"] * dt / 3.6e6
        solar += m["sensor.solar_power"] * dt / 3.6e6
        t += 300.0
    return home, solar


def _values(now: float, rng: random.Random | None = None) -> dict:
    """The state of every sensor as a string, keyed by entity id; existing ones by their key (washer, grid, today)."""
    m = model(now)
    j = (lambda v, a: v + rng.uniform(-a, a)) if rng else (lambda v, a: v)
    home_kwh, solar_kwh = _today_kwh(now)
    out = {}
    for eid, _n, _d, _a, _u, dc in SENSORS:
        if eid in m:
            out[eid] = "%.0f" % j(m[eid], 6.0)
    out["sensor.solar_energy_today"] = "%.2f" % solar_kwh
    out["sensor.electricity_price"] = "%.2f" % PRICE
    out["sensor.grid_carbon_intensity"] = "%.0f" % j(CO2, 4.0 if rng else 0)
    out["washer"] = "%.1f" % j(m["washer"], 3.0)
    out["grid"] = "%.0f" % m["grid"]
    out["today"] = "%.2f" % home_kwh
    return out


def extend(snap: dict, now: float) -> dict:
    """Append the energy devices and sensors to a demo hub snapshot and bring the existing meter sensors in line with the model."""
    vals = _values(now)
    for did, name, area in DEVICES:
        snap["devices"].append({"id": did, "name": name, "model": None, "manufacturer": None, "area": area, "via": None, "service": False,
                                "macs": [], "sw": None, "domains": ["sample_energy"], "radio": None, "role": None, "ip_hint": None,
                                "host_hint": None})
    for eid, name, did, area, unit, dc in SENSORS:
        snap["entities"].append({"entity_id": eid, "device_id": did, "name": name, "state": vals[eid], "unit": unit, "domain": "sensor",
                                 "area": area, "dc": dc, "cat": None, "platform": "sample_energy", "ls": False})
    names = {v: k for k, v in EXISTING.items()}
    for e in snap["entities"]:
        key = names.get(e.get("name"))
        if key and e.get("domain") == "sensor":
            e["state"] = vals[key]
    return snap


def tick(now: float, rng: random.Random) -> list:
    """Hub state frames with small variations for every energy sensor (the loop calls this every few seconds)."""
    vals = _values(now, rng)
    frames = []
    for eid, _n, _d, _a, _u, _dc in SENSORS:
        frames.append({"type": "hub_state", "source": "hub", "entity_id": eid, "state": vals[eid], "t": now})
    for key, eid in (("washer", "sensor.wlan_washer_2"), ("grid", "sensor.wlan_meter_1"), ("today", "sensor.wlan_meter_2")):
        frames.append({"type": "hub_state", "source": "hub", "entity_id": eid, "state": vals[key], "t": now})
    return frames


def seed(core, now: float) -> None:
    """Totals of the last five days, once and only when none exist yet."""
    book = getattr(core, "energy", None)
    if book is None or core.store.get_setting("demo_energy_seeded", False):
        return
    core.store.set_setting("demo_energy_seeded", True)
    rng = random.Random(17)
    days = {}
    for back in range(1, 6):
        d = (datetime.fromtimestamp(now) - timedelta(days=back)).strftime("%Y-%m-%d")
        kwh = 17.0 + rng.uniform(-2.5, 3.0)
        solar = 11.0 + rng.uniform(-6.0, 4.0)
        export = max(0.0, solar * rng.uniform(0.15, 0.35))
        imported = max(0.0, kwh - (solar - export))
        days[d] = {"kwh": round(kwh, 2), "cost": round(imported * PRICE - export * 0.08, 2), "co2_kg": round(imported * CO2 / 1000.0, 2),
                   "solar_kwh": round(solar, 2), "export_kwh": round(export, 2), "since": None}
    core.store.set_setting("energy_days", days)
    book._days = None
