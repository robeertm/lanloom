"""Shared fakes for the control room tests (tests/test_controlroom_*.py): a core with exactly the attributes the parts read.

Only sample data (documentation address ranges, 192.168.50.0/24); the clock is always passed in, never read from the wall.
"""
from types import SimpleNamespace

from lanloom.store import Store

T0 = 1_800_000_000.0         # a fixed "now" (a Wednesday), far from any slot boundary problems: 1_800_000_000 % 900 == 0


class FakeCore:
    def __init__(self):
        self.store = Store(":memory:")
        self.configs = []                  # entries as Core.live_configs delivers them: {key, name, age_s, state}
        self.snmp = {}
        self.gateway_ip = None
        self.hub = {}
        self.cadence = {}
        self.agents = None
        self.ip_to_device = {}
        self.merged = set()
        self.hosthealth = SimpleNamespace(readings=lambda now: {})
        self.hub_by = {}
        self.sources = []

    def live_configs(self, max_age_s=1200.0):
        return [e for e in self.configs if e["age_s"] <= max_age_s]

    def hub_merge(self):
        return {"by_hub": dict(self.hub_by)}

    def is_merged(self, dev_id):
        return dev_id in self.merged

    def source_list(self):
        return list(self.sources)


def gw_entry(wans=None, age=0, key="int:1", name="gateway", **state):
    st = dict(state)
    if wans is not None:
        st["wans"] = wans
    return {"key": key, "name": name, "age_s": age, "state": st}


def line(lid, up=None, active=None, role="single", name=None, **kw):
    d = {"id": lid, "name": name or lid, "role": role, "kind": "cable"}
    if up is not None:
        d["up"] = up
    if active is not None:
        d["active"] = active
    d.update(kw)
    return d


def hub_entry(entities, states=None, devices=None, t=T0):
    """A hub entry as the bridge keeps it: snapshot (devices, entities) plus the current states {entity_id: (value, t)}."""
    return {"snapshot": {"t": t, "devices": devices or [], "entities": entities}, "states": states or {}}


def ent(entity_id, device_id, name, unit="", dc=None, domain=None):
    return {"entity_id": entity_id, "device_id": device_id, "name": name, "unit": unit, "dc": dc, "domain": domain or entity_id.split(".")[0]}
