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


def fingerprint(state: dict) -> str:
    """Short fingerprint of the parts of a controller state a plan depends on."""
    core = {"networks": state.get("networks") or [], "zones": state.get("zones") or [], "allow": state.get("allow") or [],
            "devices": [{k: d.get(k) for k in ("id", "network", "ip")} for d in state.get("devices") or []]}
    return "fp:" + digest_hex(core)[:20]
