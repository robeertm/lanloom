"""Canonical JSON for plans and controller states: the same bytes, and therefore the same hash, in the web app,
the browser and the executor. Sorted keys, no spaces, UTF-8, no floating-point numbers."""
import hashlib
import json


class NotCanonical(ValueError):
    pass


def _check(x, depth=0):
    if depth > 12:
        raise NotCanonical("nested too deeply")
    if isinstance(x, float):
        raise NotCanonical("floating-point number")
    if isinstance(x, dict):
        for k, v in x.items():
            if not isinstance(k, str):
                raise NotCanonical("key must be text")
            _check(v, depth + 1)
    elif isinstance(x, list):
        for v in x:
            _check(v, depth + 1)
    elif not (x is None or isinstance(x, (str, int, bool))):
        raise NotCanonical("type %s" % type(x).__name__)


def canonical(obj) -> bytes:
    _check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(obj) -> bytes:
    return hashlib.sha256(canonical(obj)).digest()


def digest_hex(obj) -> str:
    return digest(obj).hex()


def _writable_fields() -> dict:
    """area -> the fields some plan step may write there (from the step registry)."""
    from .steps import KINDS
    out = {}
    for kind in KINDS.values():
        out.setdefault(kind.area, set()).update(kind.fields)
    return out


def fingerprint(state: dict) -> str:
    """Short fingerprint of the parts of a controller state a plan depends on.

    v1 parts as in earlier releases (the same states give the same fingerprint). The v2 lists count only when
    present, and only with their id and the fields a step may write - counters, link state and the like change all
    the time and must not make every plan a conflict."""
    core = {"networks": state.get("networks") or [], "zones": state.get("zones") or [], "allow": state.get("allow") or [],
            "devices": [{k: d.get(k) for k in ("id", "network", "ip")} for d in state.get("devices") or []]}
    fields = None
    for area in ("ports", "policies", "ssids", "guests", "guest_nics", "guest_rules", "bridges", "dhcp_reservations",
                 "port_forwards"):
        items = state.get(area)
        if not items:
            continue
        fields = fields or _writable_fields()
        keep = ("id",) + tuple(sorted(fields.get(area, ())))
        core[area] = [{k: it.get(k) for k in keep if k in it} for it in items if isinstance(it, dict)]
    return "fp:" + digest_hex(_no_floats(core))[:20]


def _no_floats(x, depth=0):
    """Measured values (a port's power draw, ...) may be floating-point numbers; for the fingerprint they count as
    text. States without such values give the same bytes as before."""
    if depth > 12:
        return x
    if isinstance(x, float):
        return "%.6g" % x
    if isinstance(x, dict):
        return {k: _no_floats(v, depth + 1) for k, v in x.items()}
    if isinstance(x, list):
        return [_no_floats(v, depth + 1) for v in x]
    return x
