"""Controller adapters: the only code that would ever talk to a network controller.

An adapter turns the vendor's model into the neutral state of ``model.py`` and back. Lanloom ships exactly one:
``SimulatedController``, an in-memory sample network that writes nothing anywhere. A real adapter is a small class
the operator provides and loads explicitly in the executor (see docs/FIREWALL-EDITOR.md); it is never loaded by the
web app.

Contract of an adapter (all methods synchronous, called from the executor thread):

* ``read() -> dict``            fresh neutral state; device ids must be stable opaque hashes (no hardware addresses)
* ``snapshot(ids) -> dict``     raw backup of the touched objects, JSON-serialisable, may contain internals
* ``apply(step) -> None``       one move; must be idempotent (current == after: do nothing)
* ``restore(snapshot) -> None`` put the touched objects back exactly as in the snapshot
* ``health() -> (ok, reason)``  is the controller fit for a change right now (memory, other jobs, ...)
* ``settle(steps) -> None``     wait until the controller finished provisioning (may sleep)
* ``probe(steps) -> dict``      watch probe: {"global_ok": bool, "missing": [device ids not back in the target]}
"""
import copy
import importlib
import json
import threading
import time

from .model import apply_moves


class ControllerAdapter:
    label = "controller"
    kind = "controller"

    def read(self) -> dict:
        raise NotImplementedError

    def snapshot(self, ids) -> dict:
        raise NotImplementedError

    def apply(self, step: dict) -> None:
        raise NotImplementedError

    def restore(self, snapshot: dict) -> None:
        raise NotImplementedError

    def health(self) -> tuple:
        return True, ""

    def settle(self, steps: list) -> None:
        return None

    def probe(self, steps: list) -> dict:
        return {"global_ok": True, "missing": []}


def load_adapter(spec: str, options: dict) -> ControllerAdapter:
    """``package.module:ClassName`` -> instance. Only the executor calls this, and only when writes are armed."""
    mod_name, _, cls_name = (spec or "").partition(":")
    if not mod_name or not cls_name:
        raise ValueError("adapter must look like package.module:ClassName")
    cls = getattr(importlib.import_module(mod_name), cls_name)
    inst = cls(**(options or {}))
    if not isinstance(inst, ControllerAdapter):
        raise TypeError("adapter must subclass lanloom.firewall.adapter.ControllerAdapter")
    return inst


# ---------------------------------------------------------------------------- the sample network
def _dev(i, name, kind, net, last, *, wired=False, alone=None, private=False, protected=None, fixed=True):
    return {"id": "d%02d" % i, "name": name, "kind": kind, "network": net, "ip": "", "_last": last, "fixed": fixed,
            "wired": wired, "alone_on_port": alone if wired else None, "private_address": private, "protected": protected}


_NETS = [
    {"id": "n_mgmt", "name": "Management", "zone": "z_mgmt", "cidr": "192.168.10.0/24", "vlan": 10, "management": True},
    {"id": "n_main", "name": "Main", "zone": "z_internal", "cidr": "192.168.50.0/24", "vlan": 1, "management": False},
    {"id": "n_media", "name": "Media", "zone": "z_internal", "cidr": "192.168.30.0/24", "vlan": 30, "management": False},
    {"id": "n_work", "name": "Work", "zone": "z_work", "cidr": "192.168.40.0/24", "vlan": 40, "management": False},
    {"id": "n_smart", "name": "Smart home", "zone": "z_devices", "cidr": "192.168.60.0/24", "vlan": 60, "management": False},
    {"id": "n_cams", "name": "Cameras", "zone": "z_cameras", "cidr": "192.168.70.0/24", "vlan": 70, "management": False},
    {"id": "n_guest", "name": "Guests", "zone": "z_guests", "cidr": "192.168.80.0/24", "vlan": 80, "management": False},
]
_ZONES = [
    {"id": "z_internal", "name": "Internal", "internet": True, "discovery": True},
    {"id": "z_work", "name": "Work", "internet": True, "discovery": False},
    {"id": "z_devices", "name": "Smart devices", "internet": True, "discovery": False},
    {"id": "z_cameras", "name": "Cameras", "internet": False, "discovery": False},
    {"id": "z_guests", "name": "Guests", "internet": True, "discovery": False},
    {"id": "z_mgmt", "name": "Management", "internet": True, "discovery": False},
]
_ALLOW = [["z_internal", "z_work"], ["z_internal", "z_devices"], ["z_internal", "z_cameras"], ["z_internal", "z_mgmt"],
          ["z_work", "z_devices"], ["z_mgmt", "*"]]
_DEVICES = [
    _dev(1, "Gateway", "router", "n_mgmt", 1, wired=True, alone=True, protected="fw.reason.gateway"),
    _dev(2, "Core switch", "switch", "n_mgmt", 2, wired=True, alone=True, protected="fw.reason.infrastructure"),
    _dev(3, "Access point upstairs", "access_point", "n_mgmt", 3, wired=True, alone=True, protected="fw.reason.infrastructure"),
    _dev(4, "Access point garden", "access_point", "n_mgmt", 4, wired=True, alone=True, protected="fw.reason.infrastructure"),
    _dev(5, "Storage", "storage", "n_main", 10, wired=True, alone=True, protected="fw.reason.server"),
    _dev(6, "Home server", "server", "n_main", 11, wired=True, alone=True, protected="fw.reason.self"),
    _dev(7, "Backup server", "server", "n_main", 12, wired=True, alone=True),
    _dev(8, "Office laptop", "laptop", "n_main", 20, wired=True, alone=True),
    _dev(9, "Studio desktop", "desktop", "n_main", 21, wired=True, alone=False),
    _dev(10, "Phone 1", "phone", "n_main", 22, private=True),
    _dev(11, "Kitchen tablet", "tablet", "n_main", 23),
    _dev(12, "Office printer", "printer", "n_main", 40, wired=True, alone=True),
    _dev(13, "Living room TV", "tv", "n_media", 30),
    _dev(14, "Kitchen speaker", "speaker", "n_media", 31),
    _dev(15, "Bedroom media player", "media", "n_media", 32),
    _dev(16, "Work laptop", "laptop", "n_work", 24),
    _dev(17, "Message broker", "server", "n_smart", 13, wired=True, alone=True, protected="fw.reason.hub"),
    _dev(18, "Automation hub", "hub", "n_smart", 14, wired=True, alone=True, protected="fw.reason.hub"),
    _dev(19, "Washer plug", "plug", "n_smart", 60),
    _dev(20, "Hall thermostat", "thermostat", "n_smart", 61),
    _dev(21, "Energy meter", "sensor", "n_smart", 62),
    _dev(22, "Desk light strip", "light", "n_smart", 63),
    _dev(23, "Robot vacuum", "appliance", "n_smart", 65),
    _dev(24, "Solar inverter", "sensor", "n_smart", 66, wired=True, alone=True),
    _dev(25, "Front door camera", "camera", "n_cams", 50, wired=True, alone=True),
    _dev(26, "Garage camera", "camera", "n_cams", 51),
    _dev(27, "Visitor phone", "phone", "n_guest", 23, private=True, fixed=False),
]
_DEPENDENCIES = [{"device": "d10", "by": "Automation hub (presence by address)"},
                 {"device": "d19", "by": "Automation hub"}, {"device": "d20", "by": "Automation hub"},
                 {"device": "d21", "by": "Automation hub"}, {"device": "d24", "by": "Automation hub"},
                 {"device": "d12", "by": "Office laptop (printer by address)"}]


def sample_state() -> dict:
    nets = {n["id"]: n for n in _NETS}
    devices = []
    for d in _DEVICES:
        base = nets[d["network"]]["cidr"].rsplit(".", 1)[0]
        dd = {k: v for k, v in d.items() if not k.startswith("_")}
        dd["ip"] = "%s.%d" % (base, d["_last"])
        devices.append(dd)
    return {"controller": {"label": "Sample network", "kind": "simulation"}, "zones": copy.deepcopy(_ZONES),
            "allow": copy.deepcopy(_ALLOW), "networks": copy.deepcopy(_NETS), "devices": devices,
            "dependencies": copy.deepcopy(_DEPENDENCIES), "side_effects": {"wireless_move": "reprovision_all"}}


class SimulatedController(ControllerAdapter):
    """In-memory sample network. Writes nothing outside this object (and the optional ``persist`` callback)."""

    label = "Sample network"
    kind = "simulation"

    def __init__(self, state: dict = None, persist=None, settle_s: float = 2.0, fail_devices=()):
        self._state = copy.deepcopy(state) if state else sample_state()
        self._persist = persist
        self._lock = threading.Lock()
        self.settle_s = settle_s
        self.fail_devices = set(fail_devices)      # test hook: these devices never come back after a move
        self.writes = []

    def read(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def snapshot(self, ids) -> dict:
        with self._lock:
            return {"devices": [copy.deepcopy(d) for d in self._state["devices"] if d["id"] in set(ids)]}

    def apply(self, step: dict) -> None:
        with self._lock:
            dev = next(d for d in self._state["devices"] if d["id"] == step["device"])
            if dev["network"] == step["after"]["network"] and dev["ip"] == step["after"]["ip"]:
                return
            self._state = apply_moves(self._state, [step])
            self.writes.append(step["device"])
            self._save()

    def restore(self, snapshot: dict) -> None:
        with self._lock:
            by_id = {d["id"]: d for d in snapshot.get("devices") or []}
            self._state["devices"] = [copy.deepcopy(by_id.get(d["id"], d)) for d in self._state["devices"]]
            self._save()

    def settle(self, steps: list) -> None:
        if self.settle_s:
            time.sleep(self.settle_s)

    def probe(self, steps: list) -> dict:
        return {"global_ok": True, "missing": [s["device"] for s in steps if s["device"] in self.fail_devices]}

    def reset(self) -> None:
        with self._lock:
            self._state = sample_state()
            self._save()

    def _save(self) -> None:
        if self._persist:
            try:
                self._persist(json.loads(json.dumps(self._state)))
            except Exception:
                pass
