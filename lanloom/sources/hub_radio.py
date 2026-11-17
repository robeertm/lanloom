"""Which radio network a hub device belongs to, read from its device-registry entry (hub bridge, read only).

The hub's device registry says it in technical identifiers only: the connection types (``connections``), the
integration domain of the first identifier (``identifiers``) and the domains of the device's configuration entries.
These identifier values are what the hub reports on the wire; this module is the only place that knows them, and it
turns them into neutral radio keys. Visible texts never show the identifiers (the browser translates the keys):

* ``mesh154`` - low-power 802.15.4 mesh with a coordinator (a radio stick or a network bridge),
* ``thread`` - IPv6 802.15.4 mesh with border routers,
* ``subghz`` - sub-GHz mesh with a controller,
* ``ble`` - short-range radio (BLE) heard by receivers (adapters and radio proxies),
* ``ipstd`` - devices of the IP-based smart-home standard (over Thread or wireless LAN).

Roles: ``coordinator`` (the network's own radio: stick, bridge, controller), ``border_router`` (Thread), ``receiver``
(BLE adapter or proxy), otherwise ``device``. Nothing here keeps a raw identifier: the snapshot carries only the key,
the role and, for devices of the 802.15.4 mesh, nothing more.
"""
from __future__ import annotations

KINDS = ("mesh154", "thread", "subghz", "ble", "ipstd")

# integration domains that drive one radio network each (identifier values reported by the hub)
DOMAIN_KIND = {
    "zha": "mesh154",
    "deconz": "mesh154",
    "zwave_js": "subghz",
    "zwave_me": "subghz",
    "matter": "ipstd",
    "otbr": "thread",
    "thread": "thread",
    "bluetooth": "ble",
}
# connection types of the device registry (identifier values reported by the hub)
CONNECTION_KIND = {"zigbee": "mesh154", "bluetooth": "ble"}
# first identifier of a device announced by the 802.15.4 bridge over the message broker
BRIDGE_PREFIX = "zigbee2mqtt_"
BRIDGE_SELF = "zigbee2mqtt_bridge_"


def _pairs(value) -> list:
    out = []
    for c in value if isinstance(value, list) else []:
        if isinstance(c, (list, tuple)) and len(c) == 2 and isinstance(c[0], str):
            out.append((c[0], c[1] if isinstance(c[1], str) else str(c[1])))
    return out[:16]


def classify(device: dict, domains: list) -> tuple:
    """(radio key or None, role) of one raw device-registry entry; ``domains`` are its configuration-entry domains."""
    if not isinstance(device, dict):
        return None, None
    conns = _pairs(device.get("connections"))
    idents = _pairs(device.get("identifiers"))
    kind, role = None, "device"
    for dom, value in idents:
        if dom == "mqtt" and value.startswith(BRIDGE_PREFIX):
            return "mesh154", ("coordinator" if value.startswith(BRIDGE_SELF) else "device")
    for dom in [d for d, _v in idents] + list(domains or []):
        if dom in DOMAIN_KIND:
            kind = DOMAIN_KIND[dom]
            break
    if kind is None:
        for ctype, _v in conns:
            if ctype in CONNECTION_KIND:
                kind = CONNECTION_KIND[ctype]
                break
    if kind is None:
        return None, None
    if kind == "ble" and ("bluetooth" in domains or any(d == "bluetooth" for d, _v in idents)):
        role = "receiver"                     # the radio integration registers its adapters and proxies as devices
    elif kind == "thread" and any(d in ("otbr", "thread") for d in list(domains or []) + [d for d, _v in idents]):
        role = "border_router"
    elif kind == "subghz" and any(d in ("zwave_js", "zwave_me") and _node_one(v) for d, v in idents):
        role = "coordinator"                  # node 1 of a sub-GHz network is its controller
    elif kind == "mesh154" and _coordinator_like(device):
        role = "coordinator"
    return kind, role


def _node_one(value: str) -> bool:
    parts = value.split("-")
    return len(parts) == 2 and parts[1] == "1"


def _coordinator_like(device: dict) -> bool:
    text = " ".join(str(device.get(k) or "") for k in ("model", "name")).lower()
    return "coordinator" in text
