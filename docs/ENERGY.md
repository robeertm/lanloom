# Energy

Who draws how much power right now, what it costs and how much of it is solar. The page only exists **with hub energy**: when the
home-automation hub reports no power sensor, nothing is shown and nothing is computed. It only reads what the hub bridge already holds;
no device is asked and nothing leaves the device.

## Data source and rules

Power sensors are entities with the device class *power* or the unit W, kW or MW (converted to watts). Roles come from the name:

| Role | Name contains | Sign |
|---|---|---|
| grid | grid, mains, utility, netz, import | negative = feeding into the grid |
| export | export, feed-in, return, einspeis | positive = feeding into the grid |
| solar | solar, pv, photovoltaic, inverter | production |
| battery | battery, akku, storage | positive = discharging into the home, negative = charging |
| consumer | everything else | one lane per sensor, at most 24 |

Several sensors of one role add up (phases); a sensor named *total* wins over its parts. The home draws `grid + solar + battery`; without
a grid sensor it is a house-total sensor or the sum of the consumers. The flows add up: solar covers the home first, the surplus charges
the battery and then goes to the grid; what is missing comes from the battery and then from the grid. *Rest* is what no sensor measures.

**Price and CO2.** A price entity (`EUR/kWh`, `ct/kWh`, `$/kWh` ...) and a CO2 entity (`gCO2eq/kWh`) are found by themselves. A named entity
beats a typed number, a typed number beats an entity found by itself; without any, CO2 uses 400 g/kWh and no cost is shown.
Cost per hour of a lane is `watts / 1000 * price * grid share`, CO2 per hour `watts / 1000 * g/kWh * grid share`; the grid share is
what the grid supplies of the home right now (`grid_share` in the answer, 0 to 1; 1 without a grid reading), so power from the roof or
the battery costs nothing and emits nothing at the moment it is used.

**Today.** A "today" counter of the hub (energy entity named today/daily/heute) is used when there is one. Otherwise Lanloom counts
the watts over time (trapezoid, gaps over 10 minutes are skipped) per local day. Cost counts only what came from the grid, minus the
feed-in tariff for what was exported. The last 35 day totals are kept in the site's own store (saved at most every 5 minutes and at
the day change).

## Settings and rights

`PUT /api/energy/settings` (operator; audit action `energy.settings`) takes any of `price`, `feed_in` (0..10 per kWh), `co2_g_kwh`
(0..2000), `currency` (three capital letters) and `price_entity`, `co2_entity` (an entity found among the hub's sensors, or `null`).
`GET /api/energy` is open to everybody who sees the map.

## Control room and nightly report

The control room gets an *Energy* block (home draw, solar, cost and CO2 today) that is dimmed and *unknown* when the readings are
stale. The nightly report has an *Energy* section with yesterday's consumption, cost, CO2 and solar share, only when there is a
total for that day.

## Demo data

The sample network has a solar roof that follows the daylight, a home battery, a price and a CO2 figure, and consumers such as a heat
pump, a server rack and a washer; the grid reading is always home minus solar minus battery. A few past days are seeded once.
