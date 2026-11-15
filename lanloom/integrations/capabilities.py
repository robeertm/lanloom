"""Capability lists: what an integration can read and which plan steps it may write, per device family.

Every adapter declares a static list in its manifest; after the version probe the read half may narrow it (never
widen it). The web app shows a control only when the matching write key is present - nothing is offered greyed
out. Format::

    {"family": "sample", "api": {"kind": "official-local", "version": "1.0"},
     "read":  ["networks", "zones", "clients", ...],
     "write": {"move": "B", "client.set.name": "B", "client.block": "B*", "port.set.poe": "B*"},
     "rollback": {"device_native": false, "object_snapshot": true},
     "limits": {"objects_per_plan": 10, "logins_per_hour": 4},
     "lockout": ["mgmt_network", "gateway_uplink", "self_path", "uplink_ports"]}

Write keys are a step kind (``port.set``: every field of that kind) or a step kind plus one field
(``port.set.poe``). Stages: ``B`` = allowed after the gate and a passkey signature; ``B*`` = additionally its own
confirmation with a plain warning; ``C`` = mass or structural change (create guests, host bridges), refused in this
release whatever an adapter declares (``STAGE_C_ENABLED``).
"""
import copy

from ..firewall import steps
from ..firewall.steps import MAX_OBJECTS

READ_AREAS = ("inventory", "clients", "topology", "networks", "zones", "policies", "acl", "ports", "port_profiles",
              "ssids", "wans", "vpn", "dhcp_reservations", "port_forwards", "stats", "guests", "bridges", "storage",
              "backups", "firewall")
API_KINDS = ("official-local", "classic-local", "local", "simulation")
STAGES = ("B", "B*", "C")
STAGE_RANK = {"B": 1, "B*": 2, "C": 3}
STAGE_C_ENABLED = False
LOCKOUT_RULES = ("mgmt_network", "gateway_uplink", "self_path", "uplink_ports", "own_guest", "node_bridge", "admin_interface")


class CapabilityError(ValueError):
    pass


def _write_key_ok(key: str) -> bool:
    if key in steps.KINDS:
        return True
    kind, _, field = key.rpartition(".")
    return kind in steps.KINDS and field in steps.KINDS[kind].fields and steps.KINDS[kind].verb == "set"


def validate(caps, family: str = None) -> dict:
    """Checked, normalised copy. Raises CapabilityError."""
    if not isinstance(caps, dict):
        raise CapabilityError("capabilities must be an object")
    unknown = set(caps) - {"family", "api", "read", "write", "rollback", "limits", "lockout"}
    if unknown:
        raise CapabilityError("unknown keys: %s" % ", ".join(sorted(unknown)))
    out = copy.deepcopy(caps)
    if family is not None and out.get("family") != family:
        raise CapabilityError("family mismatch")
    if not isinstance(out.get("family"), str) or not out["family"]:
        raise CapabilityError("family")
    api = out.get("api")
    if not isinstance(api, dict) or api.get("kind") not in API_KINDS or not isinstance(api.get("version", ""), str):
        raise CapabilityError("api")
    read = out.setdefault("read", [])
    if not isinstance(read, list) or set(read) - set(READ_AREAS) or len(set(read)) != len(read):
        raise CapabilityError("read areas")
    write = out.setdefault("write", {})
    if not isinstance(write, dict):
        raise CapabilityError("write")
    for key, stage in write.items():
        if not _write_key_ok(key):
            raise CapabilityError("unknown write key %s" % key)
        if stage not in STAGES:
            raise CapabilityError("stage of %s" % key)
    rb = out.setdefault("rollback", {"device_native": False, "object_snapshot": True})
    if not isinstance(rb, dict) or set(rb) - {"device_native", "object_snapshot"} or not all(isinstance(v, bool) for v in rb.values()):
        raise CapabilityError("rollback")
    lim = out.setdefault("limits", {})
    if not isinstance(lim, dict) or set(lim) - {"objects_per_plan", "logins_per_hour"}:
        raise CapabilityError("limits")
    lim.setdefault("objects_per_plan", MAX_OBJECTS)
    for k, hi in (("objects_per_plan", MAX_OBJECTS), ("logins_per_hour", 60)):
        v = lim.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= hi):
            raise CapabilityError("limit %s" % k)
    lock = out.setdefault("lockout", [])
    if not isinstance(lock, list) or set(lock) - set(LOCKOUT_RULES):
        raise CapabilityError("lockout")
    return out


def refine(caps: dict, *, version: str = None, read_missing=(), write_missing=()) -> dict:
    """Narrow a static list after the version probe. Only removes; never adds an area or a write key."""
    out = copy.deepcopy(caps)
    if version is not None:
        out["api"] = dict(out["api"], version=str(version)[:32])
    gone = set(read_missing)
    out["read"] = [a for a in out["read"] if a not in gone]
    wgone = set(write_missing)
    out["write"] = {k: v for k, v in out["write"].items() if k not in wgone and k.rpartition(".")[0] not in wgone}
    return out


def stage_for(caps: dict, step: dict):
    """The stage a step needs under these capabilities, or None when the integration cannot do it.
    Every changed field must be covered, by the whole kind or by ``kind.field``; the strictest stage wins."""
    write = (caps or {}).get("write") or {}
    kind = step.get("kind")
    if kind in write and steps.KINDS.get(kind) and steps.KINDS[kind].verb != "set":
        return write[kind]
    fields = steps.changed_fields(step)
    if not fields:
        return write.get(kind)
    worst = None
    for f in fields:
        st = write.get("%s.%s" % (kind, f)) or write.get(kind)
        if st is None:
            return None
        if worst is None or STAGE_RANK[st] > STAGE_RANK[worst]:
            worst = st
    return worst


def can(caps: dict, kind: str) -> bool:
    """Is there any write key for this step kind (whole or one field)?"""
    write = (caps or {}).get("write") or {}
    return kind in write or any(k.rpartition(".")[0] == kind for k in write)


def public(caps: dict) -> dict:
    """What the browser gets: no internals, just what to show."""
    if not caps:
        return {"read": [], "write": {}, "confirm": [], "api": {}}
    write = {k: v for k, v in caps.get("write", {}).items() if v != "C" or STAGE_C_ENABLED}
    return {"family": caps.get("family"), "api": dict(caps.get("api") or {}), "read": list(caps.get("read") or []),
            "write": write, "confirm": sorted(k for k, v in write.items() if v == "B*"),
            "objects_per_plan": (caps.get("limits") or {}).get("objects_per_plan", MAX_OBJECTS)}


# The neutral default for an adapter that declares nothing (the v1 contract: moves only).
V1_DEFAULT = {"family": "controller", "api": {"kind": "local", "version": ""}, "read": ["networks", "zones", "clients"],
              "write": {"move": "B"}, "rollback": {"device_native": False, "object_snapshot": True},
              "limits": {"objects_per_plan": MAX_OBJECTS}, "lockout": ["mgmt_network", "self_path"]}
