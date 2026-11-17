"""Controller adapters: the only code that would ever talk to a network controller.

An adapter turns the vendor's model into the neutral state of ``neutral.py`` and back. Adapters are built in only:
the write halves listed in ``lanloom.integrations.BUILTIN`` (loaded by the executor service, never by the web
app) and ``SimulatedController``, an in-memory sample network that writes nothing anywhere. There is no way to
load an adapter from elsewhere.

Contract of an adapter (all methods synchronous, called from the executor thread):

* ``read() -> dict``            fresh neutral state; object ids must be stable opaque hashes (no hardware addresses)
* ``capabilities() -> dict``    capability list (see ``lanloom.integrations.capabilities``); default: moves only
* ``snapshot(ids) -> dict``     raw backup of the touched objects, JSON-serialisable, may contain internals
* ``apply(step) -> dict|None``  one plan step; must be idempotent (current == after: do nothing). A ``*.create``
                                returns ``{"id": <the id the controller gave the new object>}``
* ``restore(snapshot) -> None`` put the touched objects back exactly as in the snapshot; ids listed in
                                ``snapshot["created"]`` were created by the plan and are removed
* ``health() -> (ok, reason)``  is the controller fit for a change right now (memory, other jobs, ...)
* ``settle(steps) -> None``     wait until the controller finished provisioning (may sleep)
* ``probe(steps) -> dict``      watch probe: {"global_ok": bool, "missing": [object ids not as planned]}
* ``lockout(state, step)``      extra family-specific lockout rule: a refusal key or None (neutral rules always run)
* ``plan_lockout(state, steps)`` the same over the whole plan (rules that depend on several steps), or None
* ``endpoints() -> list``       host names or addresses the adapter connects to; the executor's connection guard
                                allows nothing else (see ``lanloom.connguard``)
"""
import copy
import json
import threading
import time

from . import steps as stepkinds
from .model import apply_moves


class ControllerAdapter:
    label = "controller"
    kind = "controller"
    family = "controller"

    def read(self) -> dict:
        raise NotImplementedError

    def capabilities(self) -> dict:
        from ..integrations.capabilities import V1_DEFAULT
        return copy.deepcopy(V1_DEFAULT)

    def snapshot(self, ids) -> dict:
        raise NotImplementedError

    def apply(self, step: dict):
        raise NotImplementedError

    def restore(self, snapshot: dict) -> None:
        raise NotImplementedError

    def health(self) -> tuple:
        return True, ""

    def settle(self, steps: list) -> None:
        return None

    def probe(self, steps: list) -> dict:
        return {"global_ok": True, "missing": []}

    def lockout(self, state: dict, step: dict):
        return None

    def plan_lockout(self, state: dict, steps: list):
        return None

    def endpoints(self) -> list:
        return []


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


HV_LISTS = ("hosts", "guests", "guest_nics", "guest_rules", "bridges", "sdn_zones", "sdn_vnets", "fw_rules", "fw_options", "raw")
# the sample hypervisor's name server and message broker: critical guests (stopping them would cut the network off)
SAMPLE_CRITICAL = {"192.168.50.53": "hv.why.dns", "192.168.50.13": "hv.why.hub"}


def with_sample_hypervisor(state: dict, now: float = None) -> dict:
    """The sample network plus the sample hypervisor (one node, its guests, bridges and guest firewall) - the
    rehearsal target for guest changes. A state that already has hosts is returned unchanged."""
    if state.get("hosts"):
        return state
    from .. import integrations
    from ..demo_live import hypervisor_sample
    fams = integrations.families_reading("guests")
    if not fams:
        return state
    _, hv = hypervisor_sample(now or time.time(), fams[0])
    out = dict(state)
    for area in HV_LISTS:
        out[area] = copy.deepcopy(hv.get(area) or [])
    out["hints"] = {"critical_addresses": dict(SAMPLE_CRITICAL)}
    return out


def sample_state() -> dict:
    nets = {n["id"]: n for n in _NETS}
    devices = []
    for d in _DEVICES:
        base = nets[d["network"]]["cidr"].rsplit(".", 1)[0]
        dd = {k: v for k, v in d.items() if not k.startswith("_")}
        dd["ip"] = "%s.%d" % (base, d["_last"])
        dd["fixed_ip"] = dd["ip"] if d["fixed"] else ""
        devices.append(dd)
    return {"controller": {"label": "Sample network", "kind": "simulation"}, "zones": copy.deepcopy(_ZONES),
            "allow": copy.deepcopy(_ALLOW), "networks": copy.deepcopy(_NETS), "devices": devices,
            "dependencies": copy.deepcopy(_DEPENDENCIES), "side_effects": {"wireless_move": "reprovision_all"}}


class SimulatedController(ControllerAdapter):
    """In-memory sample network. Writes nothing outside this object (and the optional ``persist`` callback).

    Also the stand-in device for adapter tests: any neutral state, any capability list (default: the sample
    family's, moves only), faults on order (``fail_devices`` never come back, ``fail_after_writes`` breaks the
    n-th write, ``fail_restore`` breaks the way back)."""

    label = "Sample network"
    kind = "simulation"
    family = "simulation"

    def __init__(self, state: dict = None, persist=None, settle_s: float = 2.0, fail_devices=(), capabilities=None,
                 fail_after_writes: int = 0, fail_restore: bool = False, state_dir=None, hypervisor: bool = False):
        self.hypervisor = hypervisor              # the sample includes the sample hypervisor (the app's rehearsal target)
        self._state = copy.deepcopy(state) if state else self._initial()
        self._persist = persist
        self._lock = threading.Lock()
        self.settle_s = settle_s
        self.fail_devices = set(fail_devices)      # test hook: these devices never come back after a move
        self.fail_after_writes = fail_after_writes
        self.fail_restore = fail_restore
        self._caps = copy.deepcopy(capabilities) if capabilities else None
        self._next = 0
        self.writes = []

    def _initial(self) -> dict:
        return with_sample_hypervisor(sample_state()) if self.hypervisor else sample_state()

    def capabilities(self) -> dict:
        if self._caps:
            return copy.deepcopy(self._caps)
        from ..integrations.simulation import CAPABILITIES
        return copy.deepcopy(CAPABILITIES)

    def read(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def snapshot(self, ids) -> dict:
        ids = set(ids)
        with self._lock:
            out = {"devices": [copy.deepcopy(d) for d in self._state["devices"] if d["id"] in ids]}
            for area, items in self._state.items():
                if area != "devices" and isinstance(items, list):
                    hit = [copy.deepcopy(o) for o in items if isinstance(o, dict) and o.get("id") in ids]
                    if hit:
                        out[area] = hit
            return out

    def apply(self, step: dict):
        with self._lock:
            if self.fail_after_writes and len(self.writes) + 1 >= self.fail_after_writes:
                raise RuntimeError("simulated write failure")
            if step["kind"] == "move":
                dev = next(d for d in self._state["devices"] if d["id"] == step["device"])
                if dev["network"] == step["after"]["network"] and dev["ip"] == step["after"]["ip"]:
                    return None
                self._state = apply_moves(self._state, [step])
                self.writes.append(step["device"])
                self._save()
                return None
            kind = stepkinds.KINDS[step["kind"]]
            items = self._state.setdefault(kind.area, [])
            oid = step["object"]
            if kind.verb == "create":
                self._next += 1
                new_id = "sim%04d" % self._next
                item = dict(step["after"], id=new_id, own=True)
                if kind.area == "guest_rules":          # a sample guest rule, appended like on a real host
                    mine = [r for r in items if r.get("owner") == item.get("guest")]
                    item.update(scope="guest", owner=item.get("guest"), plain=True, direction=item.get("direction", "in"),
                                pos=1 + max([r.get("pos", 0) for r in mine] or [-1]))
                items.append(item)
                self.writes.append(new_id)
                self._sync_hv()
                self._save()
                return {"id": new_id}
            obj = next((o for o in items if o.get("id") == oid), None)
            if kind.verb == "delete":
                if obj is not None:
                    items.remove(obj)
                    self.writes.append(oid)
            elif obj is not None and any(obj.get(f) != v for f, v in step["after"].items()):
                obj.update(copy.deepcopy(step["after"]))
                if step["kind"] == "client.set":
                    obj["fixed"] = bool(obj.get("fixed_ip"))
                    if "network" in step["after"] and obj.get("fixed_ip"):
                        obj["ip"] = obj["fixed_ip"]       # the sample device takes its new address at once
                self.writes.append(oid)
            self._sync_hv()
            self._save()
            return None

    def _sync_hv(self) -> None:
        """Keep the display fields of the sample hypervisor in line with what a step wrote (the guest firewall switch
        in the options list, port and comment of a guest rule)."""
        fw = {g["id"]: g.get("firewall") for g in self._state.get("guests") or []}
        for o in self._state.get("fw_options") or []:
            if o.get("scope") == "guest" and fw.get(o.get("owner")) is not None:
                o["enabled"] = bool(fw[o["owner"]])
        for r in self._state.get("guest_rules") or []:
            if "ports" in r:
                r["dport"] = r.get("ports") or None
            if "name" in r:
                r["comment"] = ("lanloom: %s" % r["name"]).strip() if r.get("own") else (r.get("name") or None)

    def lockout(self, state: dict, step: dict):
        """The guest rules of a hypervisor family, with the sample's own critical addresses."""
        if not str(step.get("kind", "")).startswith("guest."):
            return None
        from . import guests
        crit = guests.critical_guests(state, (state.get("hints") or {}).get("critical_addresses"))
        return guests.step_lockout(state, step, crit)

    def plan_lockout(self, state: dict, steps: list):
        from . import guests
        return guests.plan_lockout(state, steps)

    def restore(self, snapshot: dict) -> None:
        with self._lock:
            if self.fail_restore:
                raise RuntimeError("simulated restore failure")
            created = set(snapshot.get("created") or [])
            by_id = {d["id"]: d for d in snapshot.get("devices") or []}
            self._state["devices"] = [copy.deepcopy(by_id.get(d["id"], d)) for d in self._state["devices"]]
            for area, saved in snapshot.items():
                if area in ("devices", "created") or not isinstance(saved, list):
                    continue
                items = self._state.setdefault(area, [])
                keep = {o["id"]: o for o in saved}
                items[:] = [copy.deepcopy(keep.pop(o.get("id"), o)) for o in items]
                items.extend(copy.deepcopy(o) for o in keep.values())     # deleted by the plan: back again
            for area, items in self._state.items():
                if created and isinstance(items, list):
                    items[:] = [o for o in items if not (isinstance(o, dict) and o.get("id") in created)]
            self._sync_hv()
            self._save()

    def settle(self, steps: list) -> None:
        if self.settle_s:
            time.sleep(self.settle_s)

    def probe(self, steps: list) -> dict:
        return {"global_ok": True, "missing": [stepkinds.step_object(s) for s in steps if stepkinds.step_object(s) in self.fail_devices]}

    def reset(self) -> None:
        with self._lock:
            self._state = self._initial()
            self._save()

    def _save(self) -> None:
        if self._persist:
            try:
                self._persist(json.loads(json.dumps(self._state)))
            except Exception:
                pass
