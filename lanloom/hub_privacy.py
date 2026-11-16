"""Privacy filter for the home-automation hub: what never leaves the bridge, and what is blacked out on the way out.

The hub knows a great deal about the people in a home: who is at home, where a phone is, camera pictures, calendar
entries, notification texts. Lanloom needs none of it to draw the network, so the bridge drops it before anything is
stored, and every way out of Lanloom (backup, export, diagnostics, AI) runs through the same filter again.

Off by default (the user may switch groups on per connection, see ``GROUPS``):

* presence:       ``person.*``, ``device_tracker.*``, ``zone.*``
* cameras:        ``camera.*``, ``image.*``
* calendars:      ``calendar.*``, ``todo.*``
* notifications:  ``persistent_notification.*``, ``notify.*``

A tracker entity is never shown, but its hardware and IP address may serve to match a hub device with a network device
(``tracker_link``) - without state ("home"), place or coordinates. Events carry only the entity id, the state and the
time; service data, user ids and context ids never leave the bridge. Attributes are dropped except the few the map
needs (``KEEP_ATTRIBUTES``).

``redact_snapshot`` / ``redact_entity_name`` black out names for exports and diagnostics even when a group is on, and
``outbound`` is the one function every outward path calls: it drops private domains whatever the user switched on.
"""
from __future__ import annotations

import ipaddress
import re

GROUPS = {
    "presence": ("person", "device_tracker", "zone"),
    "cameras": ("camera", "image"),
    "calendars": ("calendar", "todo"),
    "notifications": ("persistent_notification", "notify"),
}
PRIVATE_DOMAINS = frozenset(d for doms in GROUPS.values() for d in doms)
GROUP_FIELDS = {"privacy_" + g: g for g in GROUPS}          # integration settings (booleans, all off by default)
KEEP_ATTRIBUTES = ("unit_of_measurement", "friendly_name")
EVENT_FIELDS = ("entity_id", "state", "t")                   # the only fields of a state event that leave the bridge
MAC_RX = re.compile(r"\A[0-9a-f]{2}(?::[0-9a-f]{2}){5}\Z")
ENTITY_RX = re.compile(r"\A[a-z0-9_]{1,64}\.[a-z0-9_]{1,190}\Z")
REDACTED = "…"


def domain_of(entity_id) -> str:
    return entity_id.split(".", 1)[0] if isinstance(entity_id, str) and "." in entity_id else ""


def shown_domains(config: dict | None) -> frozenset:
    """Private domains the user explicitly switched on for one connection (empty = everything private stays out)."""
    cfg = config or {}
    out = set()
    for field, group in GROUP_FIELDS.items():
        if cfg.get(field) is True:
            out.update(GROUPS[group])
    return frozenset(out)


def is_private(entity_id, shown: frozenset = frozenset()) -> bool:
    d = domain_of(entity_id)
    return d in PRIVATE_DOMAINS and d not in shown


def valid_entity_id(entity_id) -> bool:
    return isinstance(entity_id, str) and bool(ENTITY_RX.match(entity_id))


def norm_mac(value):
    """``AA-BB-…`` / ``aa:bb:…`` -> ``aa:bb:…``; anything else -> None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower().replace("-", ":")
    return text if MAC_RX.match(text) else None


def norm_ip(value):
    if not isinstance(value, str) or len(value) > 45:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def tracker_link(entity_id, attrs) -> dict | None:
    """A tracker's hardware/IP address for matching devices - never its state, place, coordinates or name."""
    if domain_of(entity_id) != "device_tracker" or not isinstance(attrs, dict):
        return None
    mac, ip = norm_mac(attrs.get("mac")), norm_ip(attrs.get("ip"))
    if not mac and not ip:
        return None
    return {"entity_id": entity_id, "mac": mac, "ip": ip}


def device_macs(connections) -> list:
    """``connections`` of a device registry entry (``[["mac", "aa:bb:…"], …]``) -> list of hardware addresses."""
    out = []
    for c in connections if isinstance(connections, list) else []:
        if isinstance(c, (list, tuple)) and len(c) == 2 and c[0] == "mac":
            mac = norm_mac(c[1])
            if mac and mac not in out:
                out.append(mac)
    return out[:8]


def keep_attributes(attrs) -> dict:
    a = attrs if isinstance(attrs, dict) else {}
    return {k: a[k] for k in KEEP_ATTRIBUTES if k in a}


def redact_entity_name(entity: dict) -> dict:
    """An entity for an export or a diagnostic file: the entity id stays (it is a technical name), the free-text name goes."""
    out = {k: v for k, v in entity.items() if k not in ("name",)}
    out["name"] = REDACTED if entity.get("name") else None
    return out


def outbound(snapshot: dict | None) -> dict:
    """The hub data for any way out of Lanloom (export, diagnostics, AI): private domains removed whatever the user
    switched on for the map, tracker links removed, free-text names of people-related things removed."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    out = {k: v for k, v in snap.items() if k not in ("entities", "trackers", "devices")}
    out["entities"] = [e for e in snap.get("entities") or [] if isinstance(e, dict) and domain_of(e.get("entity_id")) not in PRIVATE_DOMAINS]
    out["devices"] = [{k: v for k, v in d.items() if k != "macs"} for d in snap.get("devices") or [] if isinstance(d, dict)]
    return out


def redact_snapshot(snapshot: dict | None) -> dict:
    """``outbound`` plus every free-text name blacked out (diagnostic files and exports that leave the host)."""
    snap = outbound(snapshot)
    snap["entities"] = [redact_entity_name(e) for e in snap["entities"]]
    snap["devices"] = [dict(d, name=REDACTED if d.get("name") else None) for d in snap["devices"]]
    snap["areas"] = [dict(a, name=REDACTED if a.get("name") else None) for a in snap.get("areas") or [] if isinstance(a, dict)]
    return snap


def summary(shown: frozenset) -> dict:
    """What the filter does for one connection (for the bridge card and the nightly report)."""
    return {"hidden": sorted(PRIVATE_DOMAINS - set(shown)), "shown": sorted(shown),
            "groups": {g: all(d in shown for d in doms) for g, doms in GROUPS.items()}}
