"""HTTP routes of the energy view (lanloom/energy.py): power now, flows, consumers with cost and CO2, today's totals.

Everybody who sees the map reads. An operator changes the price, the feed-in tariff, the CO2 figure and the choice of the price and
CO2 entities, the sensor that is the whole home and the sensors that are hidden (audit line ``energy.settings``). Nothing here talks to a device; the figures come from the hub bridge.

* ``GET /api/energy``                  the picture (``available`` is false without power sensors in the hub)
* ``PUT /api/energy/settings {...}``   any of price, feed_in, co2_g_kwh, currency, price_entity, co2_entity,
                                       home_entity (a power sensor that counts as the whole home), hidden (list of sensors left out); null clears one
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request


@dataclass
class Ctx:
    state: Any
    body: Callable
    err: Callable
    note: Callable


def register(app: FastAPI, c: Ctx) -> None:
    @app.get("/api/energy")
    async def energy():
        return await asyncio.to_thread(c.state.core.energy.view)

    @app.put("/api/energy/settings")
    async def energy_settings(request: Request):
        data = await c.body(request)
        try:
            cfg = c.state.core.energy.set_settings(data)
        except ValueError as exc:
            return c.err(str(exc))
        c.note(request, target="energy.settings", detail={"keys": sorted(k for k in data if k in cfg)})
        return cfg
