"""Pure mapping of controller client and device lists to topology observations (no network access).

Shared by the generic switch controller source (``sources/controller.py``) and by device-family adapters whose
classic API returns the same lists, so both feed the topology resolver with exactly the same observations.
"""
import ipaddress
import re

from ._common import clip

MAC_RE = re.compile(r"\A[0-9a-f]{2}(?::[0-9a-f]{2}){5}\Z")
ROLES = {                                       # identifier: device type codes
    "usw": "switch", "uap": "ap",               # identifier: device type codes
    "ugw": "router", "udm": "router", "uxg": "router", "gateway": "router",  # identifier: device type codes
}
BANDS = {"ng": "2.4ghz", "na": "5ghz", "6e": "6ghz"}  # identifier: radio codes


def norm_mac(value):
    if not isinstance(value, str):
        return None
    mac = value.strip().lower().replace("-", ":")
    return mac if MAC_RE.match(mac) else None


def port_str(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (int, str)):
        return clip(str(value), 32)
    return None


def ip_str(value):
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def name_str(*values):
    for v in values:
        if isinstance(v, str) and v.strip():
            return clip(v.strip(), 64)
    return None


OFFLINE_STATES = (0, 6)      # device states of the controller: 0 disconnected, 6 heartbeat missed


def map_device(d):
    if not isinstance(d, dict):
        return None
    mac = norm_mac(d.get("mac"))
    if mac is None:
        return None
    up = d.get("uplink") if isinstance(d.get("uplink"), dict) else None
    if up is None and isinstance(d.get("last_uplink"), dict):
        up = d["last_uplink"]
    uplink = None
    parent = norm_mac(up.get("uplink_mac")) if up else None
    if parent is not None:
        uplink = {"mac": parent, "port": port_str(up.get("uplink_remote_port")), "own_port": port_str(up.get("port_idx")),
                  "medium": {"wire": "wired", "wireless": "radio"}.get(up.get("type"))}
    out = {"mac": mac, "ip": ip_str(d.get("ip")), "name": name_str(d.get("name")),
           "role": ROLES.get(d.get("type"), "other"), "model": name_str(d.get("model")), "uplink": uplink}
    if d.get("state") in OFFLINE_STATES and not isinstance(d.get("state"), bool):
        out["online"] = False          # adopted but disconnected (or its heartbeat is missing): listed, but no sign of life
    return out


def map_client(c):
    if not isinstance(c, dict):
        return None
    mac = norm_mac(c.get("mac"))
    wired = c.get("is_wired")
    if mac is None or not isinstance(wired, bool):
        return None
    out = {"mac": mac, "ip": ip_str(c.get("ip")), "name": name_str(c.get("name"), c.get("hostname")),
           "medium": "wired" if wired else "radio", "parent_mac": None, "port": None, "band": None, "ssid": None}
    if wired:
        out["parent_mac"] = norm_mac(c.get("sw_mac")) or norm_mac(c.get("last_uplink_mac"))
        out["port"] = port_str(c.get("sw_port"))
    else:
        out["parent_mac"] = norm_mac(c.get("ap_mac"))
        radio = c.get("radio")
        channel = c.get("channel")
        if radio is not None:
            out["band"] = BANDS.get(radio)
        elif isinstance(channel, int) and not isinstance(channel, bool):
            out["band"] = "2.4ghz" if 1 <= channel <= 14 else "5ghz" if 36 <= channel <= 165 else None
        out["ssid"] = name_str(c.get("essid"))
    return out if out["parent_mac"] else None


def _unique(items, limit):
    seen, out = set(), []
    for it in items:
        if it is not None and it["mac"] not in seen:
            seen.add(it["mac"])
            out.append(it)
            if len(out) >= limit:
                break
    return out
