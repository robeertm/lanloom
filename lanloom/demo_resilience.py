"""Sample data of the resilience view (S13): power sources and spares of the sample network, a reserve of five minutes for a
clean shutdown and one service that needs the internet. Written once; the user's own entries are never overwritten."""
from __future__ import annotations

from . import failure, resilience

NET = "192.168.50."
HUB_HOST = 14                     # the hub runs as a guest; the sample says it needs the internet for weather and voice control
PARTS = {                         # last part of the sample address -> own fields
    1: {"power": "ups", "spare": "none", "note": "Provider box, replacement takes a few days"},
    2: {"power": "ups", "spare": "cold", "note": "Spare switch on the shelf"},
    3: {"power": "poe"},
    4: {"power": "poe"},
    10: {"spare": "partial", "note": "The backup server keeps a copy of the shares"},
    11: {"power": "ups"},
    12: {"power": "mains", "spare": "none"},
    15: {"power": "mains", "spare": "none"},
}


def seed(core, now: float) -> None:
    store = core.store
    if store.get_setting("demo_resilience_seeded", False):
        return
    store.set_setting("demo_resilience_seeded", True)
    have = resilience.parts_of(store)
    ids = getattr(core, "ip_to_device", {}) or {}
    for n, fields in PARTS.items():
        node = ids.get(NET + str(n))
        if node and node not in have:
            resilience.set_part(store, node, fields, now)
    if store.get_setting(resilience.SETTINGS, None) is None:
        store.set_setting(resilience.SETTINGS, {"runtime_min": None, "reserve_min": 5})
    hub = ids.get(NET + str(HUB_HOST))
    if hub and "internet" not in (failure.manual_entries(store).get(hub) or {}):
        failure.set_manual(store, hub, "internet", "soft", "Weather and voice control need the internet", now)
    core.failure.invalidate()
    core.resilience.invalidate()
