"""Demo: a controller configuration as a device-family adapter would read it (neutral model v2, read only).

Used by demo mode and the screenshots only. Sample names and documentation-style private addresses; no vendor
data. The firewall tab shows it as the live source "Sample controller"."""
import copy

from .firewall import neutral
from .firewall.adapter import sample_state

_EXTRA_ZONES = [
    {"id": "z_external", "name": "External", "builtin": True, "system": "external"},
    {"id": "z_gateway", "name": "Gateway", "builtin": True, "system": "gateway"},
]


def _pol(pid, name, src, dst, action, order, hits, builtin=False, **kw):
    p = {"id": pid, "name": name, "src": {"zone": src}, "dst": {"zone": dst}, "action": action, "proto": "any", "ports": "",
         "ip_version": "both", "return_traffic": action == "allow", "enabled": True, "order": order, "hits": hits,
         "last_hit": None, "logging": False, "builtin": builtin, "scheduled": False, "states": [], "editable": False,
         "vendor_ref": "v" + pid}
    for k, v in kw.items():
        if k in ("src_match", "dst_match"):
            p["src" if k == "src_match" else "dst"]["match"] = v
        else:
            p[k] = v
    return p


def live_sample() -> dict:
    st = sample_state()
    st = copy.deepcopy(st)
    st["v"] = 2
    for z in st["zones"]:
        z.setdefault("builtin", z["id"] in ("z_internal",))
        z.setdefault("system", "internal" if z["id"] == "z_internal" else None)
    st["zones"] += copy.deepcopy(_EXTRA_ZONES)
    zones = [z["id"] for z in st["zones"]]
    pols, order = [], 30000
    for a in zones:                                   # built-in defaults: inside zones reach the internet, the rest blocked
        for b in zones:
            if a == b:
                continue
            order += 1
            allow = (b == "z_external" and a not in ("z_cameras", "z_external", "z_gateway")) or b == "z_gateway" \
                or (a, b) in {("z_internal", "z_work"), ("z_internal", "z_devices"), ("z_internal", "z_cameras"),
                              ("z_internal", "z_mgmt"), ("z_work", "z_devices"), ("z_mgmt", "z_internal"), ("z_mgmt", "z_work"),
                              ("z_mgmt", "z_devices"), ("z_mgmt", "z_cameras"), ("z_mgmt", "z_guests")}
            pols.append(_pol("b%03d" % len(pols), "Allow all traffic" if allow else "Block all traffic", a, b,
                             "allow" if allow else "block", 2_000_000 + order, (len(pols) * 7919) % 400000 if allow else 0, builtin=True))
    pols.append(_pol("b_invalid", "Block invalid traffic", "z_internal", "z_external", "block", 30000, 1840, builtin=True, states=["invalid"]))
    pols += [
        _pol("u1", "Cameras to recorder only", "z_cameras", "z_internal", "allow", 10000, 902114,
             src_match={"kind": "network", "items": ["n_cam"], "opposite": False}, dst_match={"kind": "ip", "items": ["192.168.50.10"], "opposite": False},
             proto="tcp", ports="554,7447"),
        _pol("u2", "Smart devices: name lookups", "z_devices", "z_internal", "allow", 10001, 48210,
             dst_match={"kind": "ip", "items": ["192.168.50.2"], "opposite": False}, proto="udp", ports="53"),
        _pol("u3", "Guests: no local networks", "z_guests", "z_internal", "block", 10002, 312),
        _pol("u4", "Work laptop to printer", "z_work", "z_internal", "allow", 10003, 2210,
             dst_match={"kind": "ip", "items": ["192.168.50.40"], "opposite": False}, proto="tcp", ports="631,9100"),
        _pol("u5", "Block countries", "z_external", "z_internal", "block", 10004, 77310,
             src_match={"kind": "region", "items": ["AQ", "BV"], "opposite": False}),
        _pol("u6", "Old test rule", "z_work", "z_cameras", "allow", 10005, 0, enabled=False),
    ]
    st["policies"] = pols
    st["allow"] = neutral.derive_allow(st, default="block")
    ext = {"z_external"}
    for z in st["zones"]:
        z["internet"] = any(a == z["id"] and b in ext for a, b in st["allow"])
        z.setdefault("discovery", False)
    st["equipment"] = [
        {"id": "e_gw", "name": "Gateway", "kind": "router", "model": "GW-8", "firmware": "4.2.1", "mgmt_ip": "192.168.10.1", "online": True,
         "uplink": None, "stats": {"cpu": 9, "mem": 41, "uptime_s": 1_900_000, "clients": 4}},
        {"id": "e_sw1", "name": "Switch hall", "kind": "switch", "model": "SW-16P", "firmware": "7.1.3", "mgmt_ip": "192.168.10.2", "online": True,
         "uplink": {"device": "e_gw", "port": 1, "medium": "wired"}, "stats": {"cpu": 3, "mem": 38, "uptime_s": 1_200_000, "clients": 9}},
        {"id": "e_sw2", "name": "Switch office", "kind": "switch", "model": "SW-8", "firmware": "7.1.3", "mgmt_ip": "192.168.10.3", "online": True,
         "uplink": {"device": "e_sw1", "port": 16, "medium": "wired"}, "stats": {"cpu": 2, "mem": 30, "uptime_s": 1_100_000, "clients": 5}},
        {"id": "e_ap1", "name": "Access point living room", "kind": "ap", "model": "AP-6", "firmware": "6.6.8", "mgmt_ip": "192.168.10.4",
         "online": True, "uplink": {"device": "e_sw1", "port": 3, "medium": "wired"}, "stats": {"cpu": 5, "mem": 52, "uptime_s": 900_000, "clients": 11}},
    ]
    ports = []
    layout = {"e_gw": (8, {1: (1000, True, False), 2: (1000, False, False), 5: (100, False, True)}),
              "e_sw1": (16, {1: (1000, True, False), 3: (1000, False, True), 4: (1000, False, True), 7: (100, False, True),
                             9: (1000, False, False), 12: (1000, False, False), 16: (1000, False, False)}),
              "e_sw2": (8, {1: (1000, True, False), 2: (1000, False, False), 4: (100, False, False)})}
    for dev, (n, up) in layout.items():
        for i in range(1, n + 1):
            speed, uplink, poe = up.get(i, (None, False, False))
            ports.append({"id": "p_%s_%d" % (dev, i), "device": dev, "idx": i, "name": "Port %d" % i, "link": speed is not None,
                          "speed": speed, "media": "GE", "poe": {"capable": dev == "e_sw1", "on": poe, "watts": 4.2 if poe else None},
                          "profile": None, "native_vlan": 20 if (dev, i) == ("e_sw1", 7) else None, "native_network": None,
                          "tagged": ["*"] if uplink else None, "is_uplink": uplink, "infra": uplink or (dev, i) == ("e_sw1", 3),
                          "lldp_neighbor": None, "rx_bytes": None, "tx_bytes": None})
    st["ports"] = ports
    st["ssids"] = [
        {"id": "s_home", "name": "Home", "network": "n_main", "bands": ["2.4ghz", "5ghz"], "security": "wpa2_wpa3_personal", "hidden": False,
         "enabled": True, "guest": False, "isolation": False},
        {"id": "s_things", "name": "Things", "network": "n_iot", "bands": ["2.4ghz"], "security": "wpa2_personal", "hidden": True,
         "enabled": True, "guest": False, "isolation": True},
        {"id": "s_guest", "name": "Guest", "network": "n_guest", "bands": ["2.4ghz", "5ghz"], "security": "wpa2_personal", "hidden": False,
         "enabled": True, "guest": True, "isolation": True},
    ]
    st["port_forwards"] = [
        {"id": "f_vpn", "name": "Remote access", "proto": "udp", "wan_port": "51820", "dst_ip": "192.168.50.2", "dst_port": "51820",
         "enabled": True, "source_limited": False, "logging": False},
        {"id": "f_web", "name": "Web server test", "proto": "tcp", "wan_port": "8443", "dst_ip": "192.168.50.20", "dst_port": "443",
         "enabled": False, "source_limited": True, "logging": True},
    ]
    st["dhcp_reservations"] = []
    st["wans"] = [{"id": "w1", "name": "Internet 1", "type": "dhcp", "failover": None, "enabled": True}]
    st["vpn"] = []
    st["raw"] = []
    st["controller"] = {"version": "demo", "apis": ["official", "classic"], "label": "Sample controller"}
    st.pop("dependencies", None)
    st.pop("side_effects", None)
    neutral.validate(st)
    return st


def live_capabilities(family: str) -> dict:
    return {"family": family, "api": {"kind": "official-local", "version": "demo"},
            "read": ["inventory", "clients", "topology", "networks", "zones", "policies", "ports", "ssids", "port_forwards", "wans"],
            "write": {}, "rollback": {"device_native": False, "object_snapshot": True}, "limits": {"objects_per_plan": 10},
            "lockout": ["mgmt_network", "self_path"]}
