"""Demo: a controller configuration as a device-family adapter would read it (neutral model v2, read only).

Used by demo mode and the screenshots only. Sample names and documentation-style private addresses; no vendor
data. The firewall tab shows it as the live source "Sample controller"."""
import copy
import time

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


T0 = 1_700_000_000
_HIT_RATES = {"u1": 14.0, "u2": 0.9, "u3": 0.02, "u4": 0.04, "u5": 0.6, "b_invalid": 0.03}      # hits per second of the sample rules


def policy_hits(pid: str, base: int, now: float, builtin_allow: bool = False) -> int:
    """Counter of a sample rule at ``now``: grows steadily (the demo keeps counting while it runs)."""
    rate = _HIT_RATES.get(pid, 1.5 if builtin_allow else 0.0)
    return int(base + rate * max(0.0, now - T0))


def hash_slot(name: str) -> bool:
    return sum(ord(c) for c in name) % 2 == 0


def live_sample(now: float = None) -> dict:
    now = time.time() if now is None else now
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
             src_match={"kind": "network", "items": ["n_cams"], "opposite": False}, dst_match={"kind": "ip", "items": ["192.168.50.10"], "opposite": False},
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
    for p in pols:
        if p["hits"] is not None:
            outward = p["builtin"] and p["action"] == "allow" and p["dst"]["zone"] in ("z_external", "z_gateway")   # only what leaves keeps counting
            p["hits"] = policy_hits(p["id"], p["hits"], now, builtin_allow=outward)
    st["policies"] = pols
    st["ids"] = {"suppressions": 37, "enabled": True}
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
    st["equipment"][3]["radios"] = [{"band": "2.4ghz", "channel": 6, "clients": 5}, {"band": "5ghz", "channel": 44, "clients": 6}]
    st["equipment"].append({"id": "e_ap2", "name": "Access point garden", "kind": "ap", "model": "AP-6", "firmware": "6.6.8", "mgmt_ip": "192.168.10.5",
                            "online": True, "uplink": {"device": "e_sw1", "port": 4, "medium": "wired"},
                            "stats": {"cpu": 4, "mem": 47, "uptime_s": 880_000, "clients": 4},
                            "radios": [{"band": "2.4ghz", "channel": 11, "clients": 3}, {"band": "5ghz", "channel": 100, "clients": 1}]})
    layout = {"e_gw": (8, {1: (1000, True, False), 2: (1000, False, False), 5: (100, False, True)}),
              "e_sw1": (16, {1: (1000, True, False), 3: (1000, False, True), 4: (1000, False, True), 5: (1000, False, True), 6: (1000, False, True),
                             7: (100, False, True), 10: (1000, False, False), 11: (1000, False, False),
                             9: (1000, False, False), 12: (1000, False, False), 15: (10000, False, False), 16: (1000, False, False)}),
              "e_sw2": (8, {1: (1000, True, False), 2: (1000, False, False), 4: (100, False, False), 6: (100, False, False)})}
    ports = []
    for dev, (n, up) in layout.items():
        for i in range(1, n + 1):
            speed, uplink, poe = up.get(i, (None, False, False))
            link = speed is not None
            if (dev, i) == ("e_sw1", 12) and int(now // 60) % 480 == 0:          # a link that drops once in a while
                link = False
            rate = (((i * 37 + len(dev) * 11) % 90) + 5) * 20_000 if link else 0
            ports.append({"id": "p_%s_%d" % (dev, i), "device": dev, "idx": i, "name": "Port %d" % i, "link": link,
                          "speed": speed if link else None, "media": "SFP+" if (dev, i) == ("e_sw1", 15) else "GE",
                          "poe": {"capable": dev == "e_sw1", "on": poe, "watts": 4.2 if poe else None,
                                  "mode": ("auto" if poe else "off") if dev == "e_sw1" else None},
                          "profile": DEMO_PORT_PROFILE.get((dev, i), ""),
                          "profile_name": next((x["name"] for x in DEMO_PROFILES if x["id"] == DEMO_PORT_PROFILE.get((dev, i))), None),
                          "native_vlan": None, "native_network": DEMO_PORT_NET.get((dev, i), "n_mgmt" if uplink else "n_main"),
                          "tagged_mode": "all" if uplink or (dev, i) in (("e_sw1", 3), ("e_sw1", 4)) else
                          "custom" if (dev, i) == ("e_sw1", 12) else "none",
                          "tagged": ["n_media", "n_smart"] if (dev, i) == ("e_sw1", 12) else [],
                          "is_uplink": uplink, "infra": uplink or (dev, i) in (("e_sw1", 3), ("e_sw1", 4)),
                          "lldp_neighbor": "e_ap1" if (dev, i) == ("e_sw1", 3) else "e_ap2" if (dev, i) == ("e_sw1", 4) else None,
                          "rx_bytes": 5 * 10**10 + rate * int(max(0, now - T0)), "tx_bytes": 9 * 10**10 + rate * 3 * int(max(0, now - T0)),
                          "rx_errors": int(max(0, now - T0) // 43200) if (dev, i) == ("e_sw1", 7) else 0, "tx_errors": 0,
                          "rx_dropped": 0, "tx_dropped": 0, "duplex": "full" if link else None})
    vlans = {n["id"]: n.get("vlan") for n in st.get("networks") or []}
    for p in ports:
        p["native_vlan"] = vlans.get(p["native_network"])
    st["ports"] = ports
    st["port_profiles"] = [dict(x) for x in DEMO_PROFILES]
    # who sits behind which port (wired) and on which access point (wireless)
    wired = {"Storage": ("e_sw1", 9), "Home server": ("e_sw1", 12), "Backup server": ("e_sw1", 16), "Office laptop": ("e_sw2", 2),
             "Studio desktop": ("e_sw2", 4), "Office printer": ("e_sw1", 7), "Message broker": ("e_sw1", 10), "Automation hub": ("e_sw1", 11), "Solar inverter": ("e_sw2", 6), "Front door camera": ("e_sw1", 5), "Garage camera": ("e_sw1", 6)}
    for d in st["devices"]:
        if d["name"] in wired:
            d["parent"], d["port"] = wired[d["name"]]
            d["wired"] = True
        elif not d.get("wired"):
            d["parent"] = "e_ap1" if hash_slot(d["name"]) else "e_ap2"
            d["ssid"] = "Things" if d["network"] == "n_smart" else "Guest" if d["network"] == "n_guest" else "Home"
            d["blocked"] = d["name"] == "Visitor phone"          # one blocked device for the wireless section
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


# the demo switch's port profiles and which ports follow one; untagged networks of ports without a profile
DEMO_PROFILES = [
    {"id": "pp_ap", "name": "Access point trunk", "native_network": "n_mgmt", "native_vlan": None, "tagged_mode": "all", "tagged": []},
    {"id": "pp_cams", "name": "Cameras", "native_network": "n_cams", "native_vlan": None, "tagged_mode": "none", "tagged": [], "poe": True},
    {"id": "pp_smart", "name": "Smart home", "native_network": "n_smart", "native_vlan": None, "tagged_mode": "none", "tagged": []},
]
DEMO_PORT_PROFILE = {("e_sw1", 3): "pp_ap", ("e_sw1", 4): "pp_ap", ("e_sw1", 5): "pp_cams", ("e_sw1", 6): "pp_cams",
                     ("e_sw1", 10): "pp_smart", ("e_sw1", 11): "pp_smart", ("e_sw2", 6): "pp_smart"}
DEMO_PORT_NET = {("e_sw1", 3): "n_mgmt", ("e_sw1", 4): "n_mgmt", ("e_sw1", 5): "n_cams", ("e_sw1", 6): "n_cams",
                 ("e_sw1", 10): "n_smart", ("e_sw1", 11): "n_smart", ("e_sw2", 6): "n_smart"}


def live_capabilities(family: str) -> dict:
    return {"family": family, "api": {"kind": "official-local", "version": "demo"},
            "read": ["inventory", "clients", "topology", "networks", "zones", "policies", "ports", "port_profiles", "ssids",
                     "port_forwards", "wans"],
            "write": {}, "rollback": {"device_native": False, "object_snapshot": True}, "limits": {"objects_per_plan": 10},
            "lockout": ["mgmt_network", "self_path"]}


# ---------------------------------------------------------------------------- sample hypervisor
def _hv_raw(now: float) -> dict:
    """Replies of a one-node sample hypervisor (as the family's read half collects them), built from the demo
    inventory: the virtualization host 192.168.50.15 runs the broker and the automation hub as guests."""
    gib, day = 2**30, 86400
    res = [
        {"type": "node", "node": "virt-1", "status": "online", "cpu": 0.18, "maxcpu": 8, "mem": 14 * gib, "maxmem": 32 * gib, "uptime": 2_600_000},
        {"type": "qemu", "vmid": 101, "node": "virt-1", "name": "broker", "status": "running", "cpu": 0.04, "maxcpu": 2, "mem": 1.2 * gib,
         "maxmem": 2 * gib, "maxdisk": 16 * gib, "uptime": 2_590_000, "template": 0, "tags": "infra"},
        {"type": "qemu", "vmid": 102, "node": "virt-1", "name": "automation", "status": "running", "cpu": 0.11, "maxcpu": 4, "mem": 3.1 * gib,
         "maxmem": 6 * gib, "maxdisk": 64 * gib, "uptime": 2_590_000, "template": 0, "tags": "infra;home", "hastate": None},
        {"type": "lxc", "vmid": 201, "node": "virt-1", "name": "name-filter", "status": "running", "cpu": 0.01, "maxcpu": 1,
         "mem": 0.2 * gib, "maxmem": 0.5 * gib, "maxdisk": 8 * gib, "uptime": 1_200_000, "template": 0},
        {"type": "lxc", "vmid": 202, "node": "virt-1", "name": "test-box", "status": "stopped", "maxcpu": 1, "maxmem": 0.5 * gib,
         "maxdisk": 4 * gib, "template": 0},
        {"type": "qemu", "vmid": 9000, "node": "virt-1", "name": "base-template", "status": "stopped", "template": 1, "maxcpu": 2,
         "maxmem": 2 * gib, "maxdisk": 16 * gib},
    ]
    cfg = {
        "qemu/101": {"name": "broker", "agent": "1", "onboot": 1, "net0": "virtio=52:54:00:4C:00:0D,bridge=vmbr0,firewall=1"},
        "qemu/102": {"name": "automation", "agent": "1", "onboot": 1, "protection": 1,
                     "net0": "virtio=52:54:00:4C:00:0E,bridge=vmbr0,firewall=1",
                     "net1": "virtio=52:54:00:4C:00:1E,bridge=vmbr0,tag=30"},
        "lxc/201": {"hostname": "name-filter", "onboot": 1, "net0": "name=eth0,bridge=vmbr0,hwaddr=02:4C:4C:00:00:F1,ip=192.168.50.53/24,type=veth"},
        "lxc/202": {"hostname": "test-box", "net0": "name=eth0,bridge=vmbr0,tag=70,hwaddr=02:4C:4C:00:00:F2,ip=dhcp,type=veth"},
        "qemu/9000": {"name": "base-template", "template": 1, "net0": "virtio=52:54:00:4C:00:F0,bridge=vmbr0"},
    }
    rrd = [{"time": now - 120 * i, "netin": 9000 + 4000 * ((i * 7) % 5), "netout": 22000 + 9000 * ((i * 3) % 4), "cpu": 0.04} for i in range(30)]
    guests = {k: {"node": "virt-1", "config": v} for k, v in cfg.items()}
    for k in ("lxc/201", "lxc/202"):
        guests[k]["fw_options"] = {}                  # every option at its default: the guest firewall is off
    guests["qemu/101"].update({"agent": {"result": [{"name": "eth0", "hardware-address": "52:54:00:4c:00:0d",
                                                     "ip-addresses": [{"ip-address": "192.168.50.13"}]}]}, "rrd": rrd,
                               "fw_options": {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT"},
                               "fw_rules": [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": "1883,8883",
                                             "source": "192.168.50.0/24", "comment": "message broker"}]})
    guests["qemu/102"].update({"agent": {"result": [{"name": "eth0", "hardware-address": "52:54:00:4c:00:0e",
                                                     "ip-addresses": [{"ip-address": "192.168.50.14"}]}]}, "rrd": rrd,
                               "fw_options": {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT"},
                               "fw_rules": [{"pos": 0, "type": "group", "action": "web-ui", "enable": 1},
                                            {"pos": 1, "type": "in", "action": "ACCEPT", "enable": 1, "macro": "SSH", "source": "+admins"}]})
    return {
        "api_host": "192.168.50.15", "version": {"version": "8.2"},
        "cluster": [{"type": "node", "name": "virt-1", "ip": "192.168.50.15", "online": 1, "local": 1}],
        "resources": res, "guests": guests,
        "nodes": {"virt-1": {
            "status": {"cpu": 0.18, "loadavg": ["0.62", "0.55", "0.48"], "memory": {"total": 32 * gib, "used": 14 * gib},
                       "uptime": 2_600_000, "pveversion": "manager/8.2/demo", "cpuinfo": {"cpus": 8}},
            "network": [{"iface": "eno1", "type": "eth", "active": 1, "autostart": 1},
                        {"iface": "vmbr0", "type": "bridge", "active": 1, "autostart": 1, "bridge_ports": "eno1", "bridge_vlan_aware": 1,
                         "bridge_vids": "2-4094", "cidr": "192.168.50.15/24", "gateway": "192.168.50.1"},
                        {"iface": "vmbr1", "type": "bridge", "active": 1, "autostart": 1, "bridge_ports": "", "comments": "isolated lab"}],
            "storage": [{"storage": "local", "type": "dir", "active": 1, "total": 100 * gib, "used": 31 * gib, "content": "iso,vztmpl"},
                        {"storage": "local-fast", "type": "lvmthin", "active": 1, "total": 480 * gib, "used": 302 * gib, "content": "images,rootdir"},
                        {"storage": "backup-share", "type": "nfs", "active": 1, "total": 4000 * gib, "used": 3720 * gib, "content": "backup", "shared": 1}],
            "fw_options": {"enable": 1}, "fw_rules": [],
            "tasks": [{"type": "vzdump", "id": "", "endtime": now - 5 * 3600, "status": "OK"}]}},
        "dc_fw": {"options": {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT"},
                  "rules": [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": "8006", "source": "+admins",
                             "comment": "management page"},
                            {"pos": 1, "type": "in", "action": "ACCEPT", "enable": 1, "macro": "SSH", "source": "+admins"}],
                  "groups": [{"group": "web-ui", "comment": "web front ends"}],
                  "group_rules": {"web-ui": [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": "8123"}]},
                  "aliases": [{"name": "gateway", "cidr": "192.168.50.1"}],
                  "ipsets": [{"name": "admins"}], "ipset_members": {"admins": [{"cidr": "192.168.50.20"}, {"cidr": "192.168.50.21"}]}},
        "sdn": {"zones": [{"zone": "home", "type": "vlan", "bridge": "vmbr0"}],
                "vnets": [{"vnet": "things", "zone": "home", "tag": 30, "alias": "Smart devices"}],
                "subnets": {"things": [{"cidr": "192.168.30.0/24", "gateway": "192.168.30.1"}]}},
        "storage_cfg": [], "not_backed_up": [{"vmid": 201}],
        "backup_jobs": [{"id": "backup-demo-1", "enabled": 1, "schedule": "02:00", "storage": "backup-share", "mode": "snapshot",
                         "vmid": "101,102", "next-run": now + 9 * 3600, "comment": "nightly"}],
        "backups": [{"vmid": 101, "ctime": now - 0.8 * day, "size": 3 * gib, "storage": "backup-share", "verification": {"state": "ok"}},
                    {"vmid": 102, "ctime": now - 0.8 * day, "size": 11 * gib, "storage": "backup-share"}],
        "traffic": {"qemu/101": [14_000, 31_000], "qemu/102": [52_000, 160_000], "lxc/201": [3_000, 2_500]},
    }


def hypervisor_sample(now: float, family: str) -> tuple:
    """(topology event, neutral state) of the sample hypervisor, made by the family's own translation module."""
    from . import integrations
    hv = integrations.load_model(family)
    raw = _hv_raw(now)
    st = hv.to_neutral(raw, now)
    st["controller"]["label"] = "Sample hypervisor"
    st["controller"]["version"] = "demo"
    neutral.validate(st)
    return hv.topology_event(raw, now), st


def hypervisor_capabilities(family: str) -> dict:
    return {"family": family, "api": {"kind": "official-local", "version": "demo"},
            "read": ["inventory", "topology", "guests", "bridges", "networks", "firewall", "storage", "backups", "stats"],
            "write": {}, "rollback": {"device_native": False, "object_snapshot": True}, "limits": {"objects_per_plan": 10},
            "lockout": ["own_guest", "node_bridge", "mgmt_network"]}


# ---------------------------------------------------------------------------- sample firewall
def _fw_reply(extra_rule: bool) -> bytes:
    """One reply document of a sample zone firewall's query API (as the family's read half receives it), built from
    the demo network: the router 192.168.50.1 with a home zone, a zone for smart devices, a DMZ for the web front end
    and the internet side. ``extra_rule`` adds the rule and the forwarding that "arrived tonight" in the demo report."""
    zones = "".join(
        "<Zone><Name>%s</Name><Type>%s</Type><ApplianceAccess><AdminServices>%s</AdminServices></ApplianceAccess></Zone>" % z
        for z in (("Home", "LAN", "<HTTPS>HTTPS</HTTPS><SSH>SSH</SSH>"), ("Internet", "WAN", "<HTTPS>HTTPS</HTTPS>"),
                  ("Smart", "LAN", ""), ("Front", "DMZ", ""), ("Remote", "VPN", "")))
    ifaces = ("<Interface><IPv4Configuration>Enable</IPv4Configuration><Name>lan</Name><Hardware>lan</Hardware><NetworkZone>Home</NetworkZone>"
              "<IPv4Assignment>Static</IPv4Assignment><IPAddress>192.168.50.1</IPAddress><Netmask>255.255.255.0</Netmask></Interface>"
              "<Interface><IPv4Configuration>Enable</IPv4Configuration><Name>wan</Name><Hardware>wan</Hardware><NetworkZone>Internet</NetworkZone>"
              "<IPv4Assignment>PPPoE</IPv4Assignment></Interface>"
              "<Interface><IPv4Configuration>Enable</IPv4Configuration><Name>dmz</Name><Hardware>dmz</Hardware><NetworkZone>Front</NetworkZone>"
              "<IPv4Assignment>Static</IPv4Assignment><IPAddress>192.168.80.1</IPAddress><Netmask>255.255.255.0</Netmask></Interface>"
              "<VLAN><Zone>Smart</Zone><Interface>lan</Interface><Hardware>lan.30</Hardware><Name>lan.30</Name><VLANID>30</VLANID>"
              "<IPv4Configuration>Enable</IPv4Configuration><IPv4Assignment>Static</IPv4Assignment><IPAddress>192.168.30.1</IPAddress>"
              "<Netmask>255.255.255.0</Netmask></VLAN>")
    objects = ("<IPHost><Name>broker</Name><HostType>IP</HostType><IPAddress>192.168.50.13</IPAddress></IPHost>"
               "<IPHost><Name>front-end</Name><HostType>IP</HostType><IPAddress>192.168.80.10</IPAddress></IPHost>"
               "<IPHost><Name>camera-box</Name><HostType>IP</HostType><IPAddress>192.168.50.40</IPAddress></IPHost>"
               "<Services><Name>MQTT-TLS</Name><Type>TCPorUDP</Type><ServiceDetails><ServiceDetail><DestinationPort>8883</DestinationPort>"
               "<Protocol>TCP</Protocol></ServiceDetail></ServiceDetails></Services>"
               "<Services><Name>HTTPS</Name><Type>TCPorUDP</Type><ServiceDetails><ServiceDetail><DestinationPort>443</DestinationPort>"
               "<Protocol>TCP</Protocol></ServiceDetail></ServiceDetails></Services>"
               "<Services><Name>HTTP</Name><Type>TCPorUDP</Type><ServiceDetails><ServiceDetail><DestinationPort>80</DestinationPort>"
               "<Protocol>TCP</Protocol></ServiceDetail></ServiceDetails></Services>"
               "<Services><Name>RTSP</Name><Type>TCPorUDP</Type><ServiceDetails><ServiceDetail><DestinationPort>554</DestinationPort>"
               "<Protocol>TCP</Protocol></ServiceDetail></ServiceDetails></Services>"
               "<ServiceGroup><Name>Web</Name><ServiceList><Service>HTTP</Service><Service>HTTPS</Service></ServiceList></ServiceGroup>")

    def rule(name, src, dst, action="Accept", services=(), dst_net=(), status="Enable", schedule="All The Time"):
        return ("<FirewallRule><Name>%s</Name><Status>%s</Status><PolicyType>Network</PolicyType><NetworkPolicy><Action>%s</Action>"
                "<SourceZones>%s</SourceZones><DestinationZones>%s</DestinationZones><Schedule>%s</Schedule>"
                "<DestinationNetworks>%s</DestinationNetworks><Services>%s</Services></NetworkPolicy></FirewallRule>") % (
            name, status, action, "".join("<Zone>%s</Zone>" % z for z in src), "".join("<Zone>%s</Zone>" % z for z in dst), schedule,
            "".join("<Network>%s</Network>" % n for n in dst_net), "".join("<Service>%s</Service>" % s for s in services))

    rules = (rule("Home to internet", ["Home"], ["Internet"])
             + rule("Smart devices may reach the broker", ["Smart"], ["Home"], services=["MQTT-TLS"], dst_net=["broker"])
             + rule("Smart devices to internet (web)", ["Smart"], ["Internet"], services=["Web"])
             + rule("Home and remote to front end", ["Home", "Remote"], ["Front"])
             + rule("Front end updates", ["Front"], ["Internet"], services=["Web"], schedule="Nights")
             + rule("Old test rule", [], [], status="Disable")
             + rule("Block smart devices to front end", ["Smart"], ["Front"], action="Drop"))
    nat = ("<NATRule><Name>Web front end</Name><Status>Enable</Status><TranslatedDestination>front-end</TranslatedDestination>"
           "<TranslatedService>Original</TranslatedService><OriginalServices><Service>HTTPS</Service></OriginalServices>"
           "<TranslatedSource>Original</TranslatedSource></NATRule>"
           "<NATRule><Name>Default source NAT</Name><Status>Enable</Status><TranslatedDestination>Original</TranslatedDestination>"
           "<TranslatedSource>MASQ</TranslatedSource></NATRule>")
    if extra_rule:
        rules += rule("Camera stream from outside", ["Internet"], ["Home"])
        nat += ("<NATRule><Name>Camera stream</Name><Status>Enable</Status><TranslatedDestination>camera-box</TranslatedDestination>"
                "<TranslatedService>Original</TranslatedService><OriginalServices><Service>RTSP</Service></OriginalServices></NATRule>")
    other = ("<DHCPServer><Name>Home addresses</Name><Interface>lan - 192.168.50.1</Interface><Status>Enable</Status>"
             "<StaticIPMapping><MACIPMapping><HostName>printer</HostName><MACAddress>02:4c:4c:00:00:31</MACAddress>"
             "<IPAddress>192.168.50.31</IPAddress></MACIPMapping></StaticIPMapping></DHCPServer>"
             "<DHCPServer><Name>Smart addresses</Name><Interface>lan.30 - 192.168.30.1</Interface><Status>Enable</Status></DHCPServer>"
             "<UnicastRoute><DestinationIP>10.20.0.0</DestinationIP><Netmask>255.255.0.0</Netmask><Gateway>192.168.50.2</Gateway>"
             "<Status>Enable</Status></UnicastRoute>"
             "<VPNIPSecConnection><Name>Holiday flat</Name><ConnectionType>SiteToSite</ConnectionType><Status>Active</Status></VPNIPSecConnection>"
             "<SSLVPNPolicy><Name>Phones on the road</Name></SSLVPNPolicy>")
    return ('<?xml version="1.0" encoding="UTF-8"?><Response APIVersion="demo"><Login><status>Authentication Successful</status></Login>'
            + zones + ifaces + objects + rules + nat + other + "</Response>").encode("utf-8")


def firewall_sample(family: str, extra_rule: bool = False) -> dict:
    """Neutral state of the sample firewall, made by the family's own translation module from a sample reply."""
    from . import integrations
    fw = integrations.load_model(family)
    meta, ents = fw.parse_reply(_fw_reply(extra_rule))
    st = fw.to_neutral({"api_version": meta["api_version"], "entities": ents})
    st["controller"]["label"] = "Sample firewall"
    neutral.validate(st)
    return st


def firewall_capabilities(family: str) -> dict:
    return {"family": family, "api": {"kind": "official-local", "version": "demo"},
            "read": ["zones", "networks", "policies", "port_forwards", "dhcp_reservations", "wans", "vpn", "firewall"],
            "write": {}, "rollback": {"device_native": False, "object_snapshot": True}, "limits": {"objects_per_plan": 10},
            "lockout": ["mgmt_network", "gateway_uplink", "self_path", "admin_interface"]}


# ---------------------------------------------------------------------------- sample gateway firewall (rule API)
def _gw_raw(changed: bool) -> dict:
    """Collected replies of a sample gateway firewall with a rule API (interfaces as zones, rules on interfaces) as its
    family's read half hands them to the translation module, built from the demo network: a lab gateway 192.168.50.254
    with the home network, a smart-device VLAN, a guest VLAN, a lab and the internet side. ``changed`` is what
    "happened tonight" in the demo report: a rule from the internet side to the lab arrived and a rule was switched off."""
    ifaces = [("wan", "Internet", "203.0.113.10/24", None, "dhcp", ["Internet uplink"]),
              ("lan", "Home", "192.168.50.254/24", None, "static", []),
              ("opt1", "Smart devices", "192.168.30.254/24", 30, "static", []),
              ("opt2", "Guests", "192.168.80.254/24", 80, "static", []),
              ("opt3", "Lab", "192.168.90.254/24", None, "static", [])]
    rows = [{"identifier": i, "description": d, "addr4": a, "vlan_tag": str(v) if v else None, "link_type": l, "gateways": g,
             "enabled": True, "status": "up"} for i, d, a, v, l, g in ifaces]

    def rule(n, desc, iface, *, action="pass", src="", dst="any", proto="any", port="", enabled="1"):
        return ("a0d0%04d-0000-4000-8000-%012d" % (n, n),
                {"enabled": enabled, "sequence": str(n * 10), "action": action, "quick": "1", "interface": iface,
                 "direction": "in", "ipprotocol": "inet", "protocol": proto, "source_net": src or iface or "any",
                 "source_not": "0", "destination_net": dst, "destination_not": "0", "destination_port": port,
                 "description": desc, "statetype": "keep"})

    rules = [rule(1, "Home to anywhere", "lan"),
             rule(2, "Smart devices to the broker", "opt1", dst="192.168.50.13", proto="TCP", port="8883"),
             rule(3, "Smart devices may not reach home", "opt1", action="block", dst="lan"),
             rule(4, "Smart devices to the web", "opt1", proto="TCP", port="443"),
             rule(5, "Guests may not reach home", "opt2", action="block", dst="lan"),
             rule(11, "Guests may not reach smart devices", "opt2", action="block", dst="opt1"),
             rule(12, "Guests may not reach the lab", "opt2", action="block", dst="opt3"),
             rule(13, "Guests may ask the firewall for names", "opt2", dst="(self)", proto="TCP/UDP", port="53"),
             rule(14, "Inside may ask the firewall for names", "inside", src="any", dst="(self)", proto="TCP/UDP", port="53"),
             rule(6, "Guests to the internet", "opt2"),
             rule(7, "Home to the lab", "lan", dst="opt3", enabled="0" if changed else "1"),
             rule(8, "Firewall admin from outside", "wan", src="any", dst="(self)", proto="TCP", port="443"),
             rule(9, "[lanloom] Lab may not reach smart devices", "opt3", action="block", dst="opt1")]
    if changed:
        rules.append(rule(15, "Lab test from outside", "wan", src="any", dst="opt3"))
    for pos, (_, r) in enumerate(rules, start=1):            # evaluated in the order written above
        r["sequence"] = str(pos * 10)
    return {"version": "demo", "interfaces": {"rows": rows}, "filter": {"rules": {"rule": dict(rules)}},
            "groups": {"rows": [{"ifname": "Inside", "members": "lan,opt1", "descr": "Home and smart devices"}]},
            "dnat": {"rows": [{"descr": "Lab web server", "disabled": "0", "protocol": "TCP", "destination.port": "8443",
                               "target": "192.168.90.20", "local-port": "443"}]},
            "kea": {"rows": [{"ip_address": "192.168.50.40", "hw_address": "02:4c:4c:00:00:40", "hostname": "office-printer"}]},
            "routes": {"rows": [{"network": "10.70.0.0/16", "gateway": "Lab router", "disabled": "0"}]},
            "gateways": {"items": [{"name": "Internet uplink", "status": "none"}]},
            "vpn_tunnel": {"rows": [{"name": "Phones on the road", "enabled": "1"}]},
            "vpn_ipsec": {"rows": [{"description": "Holiday flat", "enabled": "1"}]}}


def gateway_sample(family: str, changed: bool = False) -> dict:
    """Neutral state of the sample gateway firewall, made by the family's own translation module."""
    from . import integrations
    st = integrations.load_model(family).to_neutral(_gw_raw(changed))
    st["controller"]["label"] = "Sample gateway firewall"
    st["controller"]["version"] = "demo"
    from . import demo_controlroom
    import time as _time
    demo_controlroom.extend_gateway(st, _time.time())
    neutral.validate(st)
    return st


def gateway_capabilities(family: str) -> dict:
    return {"family": family, "api": {"kind": "official-local", "version": "demo"},
            "read": ["zones", "networks", "policies", "port_forwards", "dhcp_reservations", "wans", "vpn", "firewall"],
            "write": {}, "rollback": {"device_native": False, "object_snapshot": True}, "limits": {"objects_per_plan": 10},
            "lockout": ["mgmt_network", "gateway_uplink", "self_path", "admin_interface"]}


# ---------------------------------------------------------------------------- sample router (ubus API)
def _uci_values(sections: list) -> dict:
    """A list of (section name or None, type, options) as a ``uci get`` reply's values (unnamed sections get a name
    the way the router hands them out)."""
    out = {}
    for i, (name, kind, opts) in enumerate(sections):
        sec = {".type": kind, ".anonymous": name is None, ".name": name or "cfg%02x%04x" % (i + 1, (i * 7919) % 65536),
               ".index": i}
        sec.update(opts)
        out[sec[".name"]] = sec
    return out


def _rt_raw(changed: bool, m) -> dict:
    """Collected replies of a sample router with a ubus API as its family's read half hands them to the translation
    module (``m``, whose constants name the configuration's section types): a lab router 192.168.90.1 with a home network, a network for smart devices, a guest network and the
    internet side (the demo house network). ``changed`` is what "happened tonight" in the demo report: a rule from the
    internet side arrived, a rule was switched off and the guest wireless network was switched on."""
    network = [("loopback", "interface", {"device": "lo", "proto": "static", "ipaddr": "127.0.0.1"}),
               (None, "device", {"name": "br-lan", "type": "bridge", "ports": ["lan1", "lan2", "lan3"]}),
               ("lan", "interface", {"device": "br-lan", "proto": "static", "ipaddr": ["192.168.90.1/24"]}),
               ("smart", "interface", {"device": "br-lan.91", "proto": "static", "ipaddr": ["192.168.91.1/24"]}),
               ("guest", "interface", {"device": "br-guest", "proto": "static", "ipaddr": ["192.168.92.1/24"]}),
               ("wan", "interface", {"device": "wan", "proto": "dhcp"})]

    def rule(name, src, dest=None, *, target="ACCEPT", proto=None, port=None, enabled=None, section=None, **extra):
        o = {"name": name, "src": src, "target": target}
        if dest:
            o["dest"] = dest
        if proto:
            o["proto"] = proto
        if port:
            o["dest_port"] = port
        if enabled is not None:
            o["enabled"] = enabled
        o.update(extra)
        return (section, "rule", o)

    firewall = [(None, "defaults", {"input": "REJECT", "output": "ACCEPT", "forward": "REJECT"}),
                (None, "zone", {"name": "lan", "network": ["lan"], "input": "ACCEPT", "output": "ACCEPT", "forward": "ACCEPT"}),
                (None, "zone", {"name": "smart", "network": ["smart"], "input": "REJECT", "output": "ACCEPT", "forward": "REJECT"}),
                (None, "zone", {"name": "guest", "network": ["guest"], "input": "REJECT", "output": "ACCEPT", "forward": "REJECT"}),
                (None, "zone", {"name": "wan", "network": ["wan"], "input": "REJECT", "output": "ACCEPT", "forward": "REJECT",
                                "masq": "1"}),
                (None, "forwarding", {"src": "lan", "dest": "wan"}),
                (None, "forwarding", {"src": "smart", "dest": "wan"}),
                (None, "forwarding", {"src": "guest", "dest": "wan"}),
                rule("Renew the internet address", "wan", proto="udp", port="68", family="ipv4"),
                rule("Answer ping from outside", "wan", proto="icmp", icmp_type="echo-request", family="ipv4"),
                rule("Smart devices: name service", "smart", port="53"),
                rule("Smart devices: address service", "smart", proto="udp", port="67"),
                rule("Smart devices to the broker", "smart", "lan", proto="tcp", port="8883", dest_ip="192.168.90.13"),
                rule("Guests may not reach home", "guest", "lan", target="REJECT", proto="all"),
                rule("Guests: name service", "guest", port="53"),
                rule("Home to smart devices", "lan", "smart", proto="all", enabled="0" if changed else None),
                rule("[lanloom] Smart devices may not reach guests", "smart", "guest", target="DROP", proto="all",
                     section="lanloom_0d0e0m0o0r01")]
    if changed:
        firewall.append(rule("Remote access test", "wan", proto="tcp", port="22"))
    firewall.append((None, "redirect", {"name": "Lab camera", "target": "DNAT", "src": "wan", "src_dport": "8554",
                                        "dest": "lan", "dest_ip": "192.168.90.30", "dest_port": "554", "proto": "tcp"}))
    dhcp = [("lan", "dhcp", {"interface": "lan", "start": "100", "limit": "150"}),
            ("smart", "dhcp", {"interface": "smart", "start": "20", "limit": "100"}),
            ("guest", "dhcp", {"interface": "guest", "start": "10", "limit": "200"}),
            (None, "host", {"name": "lab-printer", "mac": "02:4c:4c:00:90:40", "ip": "192.168.90.40"})]
    wireless = [("radio0", m.RADIO_SECTION, {"band": "2g", "channel": "6"}),
                ("radio1", m.RADIO_SECTION, {"band": "5g", "channel": "36"}),
                ("home2", m.AP_SECTION, {"device": "radio0", "network": "lan", "mode": "ap", "ssid": "Lab", "encryption": "psk2"}),
                ("home5", m.AP_SECTION, {"device": "radio1", "network": "lan", "mode": "ap", "ssid": "Lab", "encryption": "sae-mixed"}),
                ("smart", m.AP_SECTION, {"device": "radio0", "network": "smart", "mode": "ap", "ssid": "Lab things",
                                         "encryption": "psk2", "hidden": "1"}),
                ("guest", m.AP_SECTION, {"device": "radio0", "network": "guest", "mode": "ap", "ssid": "Lab guests",
                                         "encryption": "none", "isolate": "1", "disabled": "0" if changed else "1"})]
    dump = {"interface": [{"interface": "lan", "up": True, "l3_device": "br-lan", "ipv4-address": [{"address": "192.168.90.1", "mask": 24}]},
                          {"interface": "smart", "up": True, "l3_device": "br-lan.91", "ipv4-address": [{"address": "192.168.91.1", "mask": 24}]},
                          {"interface": "guest", "up": True, "l3_device": "br-guest", "ipv4-address": [{"address": "192.168.92.1", "mask": 24}]},
                          {"interface": "wan", "up": True, "l3_device": "wan", "ipv4-address": [{"address": "192.168.50.90", "mask": 24}]}]}
    air = {"phy0-ap0": {"info": {"ssid": "Lab", "frequency": 2437, "mode": "Master"},
                         "assoc": [{"mac": "02:4c:4c:00:90:51", "signal": -48}]},
            "phy1-ap0": {"info": {"ssid": "Lab", "frequency": 5180, "mode": "Master"},
                         "assoc": [{"mac": "02:4c:4c:00:90:52", "signal": -55}, {"mac": "02:4c:4c:00:90:53", "signal": -61}]},
            "phy0-ap1": {"info": {"ssid": "Lab things", "frequency": 2437, "mode": "Master"},
                         "assoc": [{"mac": "02:4c:4c:00:91:61", "signal": -67}]}}
    if changed:
        air["phy0-ap2"] = {"info": {"ssid": "Lab guests", "frequency": 2437, "mode": "Master"}, "assoc": []}
    leases = {"dhcp_leases": [{"hostname": "lab-tablet", "ipaddr": "192.168.90.151", "macaddr": "02:4c:4c:00:90:51"},
                              {"hostname": "lab-laptop", "ipaddr": "192.168.90.152", "macaddr": "02:4c:4c:00:90:52"},
                              {"hostname": "test-phone", "ipaddr": "192.168.90.153", "macaddr": "02:4c:4c:00:90:53"},
                              {"hostname": "lab-sensor", "ipaddr": "192.168.91.61", "macaddr": "02:4c:4c:00:91:61"}]}
    return {"version": "demo", "board": {"hostname": "lab-router", "model": "Sample router", "release": {"version": "demo"}},
            "network": _uci_values(network), "firewall": _uci_values(firewall), "dhcp": _uci_values(dhcp),
            "wireless": _uci_values(wireless), "ifaces": dump, "devices": {"br-lan": {"macaddr": "02:4c:4c:00:90:01"}},
            "hints": {"02:4c:4c:00:90:40": {"name": "lab-printer", "ipaddrs": ["192.168.90.40"]}}, "leases": leases, "air": air}


def router_sample(family: str, changed: bool = False) -> dict:
    """Neutral state of the sample router, made by the family's own translation module."""
    from . import integrations
    m = integrations.load_model(family)
    st = m.to_neutral(_rt_raw(changed, m))
    st["controller"]["label"] = "Sample router"
    st["controller"]["version"] = "demo"
    neutral.validate(st)
    return st


def router_capabilities(family: str) -> dict:
    return {"family": family, "api": {"kind": "official-local", "version": "demo"},
            "read": ["inventory", "clients", "networks", "zones", "policies", "ssids", "wans", "dhcp_reservations",
                     "port_forwards", "firewall"],
            "write": {}, "rollback": {"device_native": False, "object_snapshot": True},
            "limits": {"objects_per_plan": 10}, "lockout": ["mgmt_network", "gateway_uplink", "self_path", "admin_interface"]}


# ---------------------------------------------------------------------------- sample access-point controller
def _ctl_raw(changed: bool = False) -> dict:
    """Collected replies of a sample access-point controller as its family's read half hands them to the translation
    module, built from the demo network (gateway 192.168.50.1, a switch and two access points, three networks, three
    wireless networks, three access rules). ``changed`` is what "happened tonight" in the demo report: one access point
    went offline, got a new firmware, and a rule arrived."""
    def mac(n):
        return "02-4C-4C-00-00-%02X" % n

    gw, sw, ap1, ap2 = mac(0xA1), mac(0xA2), mac(0xA3), mac(0xA4)

    def dev(m, name, kind, ip, up="", port="", status=1, fw="2.1.0", model="Sample"):
        return {"mac": m, "name": name, "type": kind, "ip": ip, "status": status, "model": model, "modelName": model,
                "firmwareVersion": fw, "uptime": "12day(s) 4h 3m 2s", "cpuUtil": 5, "memUtil": 38, "uplinkDeviceMac": up,
                "uplinkDevicePort": port}

    devices = [dev(gw, "Sample gateway", "gateway", "192.168.50.1", model="GW-1"),
               dev(sw, "Sample switch", "switch", "192.168.50.2", gw, "5", model="SW-8P"),
               dev(ap1, "Sample AP upstairs", "ap", "192.168.50.3", sw, "2", model="AP-2", fw="2.4.1" if changed else "2.4.0"),
               dev(ap2, "Sample AP garden", "ap", "192.168.50.4", sw, "3", model="AP-2", status=0 if changed else 1)]

    def cl(n, name, ip, wired, parent, **kw):
        base = {"mac": mac(n), "name": name, "ip": ip, "active": True, "wireless": not wired, "connectType": 2 if wired else 1,
                "guest": False, "blocked": False}
        base.update({"switchMac": parent, "connectDevType": "switch"} if wired else {"apMac": parent, "connectDevType": "ap"})
        base.update(kw)
        return base

    clients = [cl(0x10, "Media server", "192.168.50.20", True, sw, port=4, networkName="Home",
                  ipSetting={"useFixedAddr": True}),
               cl(0x11, "Office printer", "192.168.50.31", True, sw, port=6, networkName="Home"),
               cl(0x12, "Garden camera", "192.168.30.40", True, sw, port=7, networkName="Smart"),
               cl(0x13, "Kitchen tablet", "192.168.50.52", False, ap1, ssid="Home", radioId=1),
               cl(0x14, "Living room speaker", "192.168.30.43", False, ap1, ssid="Smart", radioId=0),
               cl(0x15, "Visitor phone", "192.168.70.12", False, ap1, ssid="Visitors", radioId=1, connectType=0, guest=True)]
    if not changed:
        clients.append(cl(0x16, "Patio sensor", "192.168.30.44", False, ap2, ssid="Smart", radioId=0))
    nets = [{"id": "n-home", "name": "Home", "purpose": 1, "vlanType": 0, "vlan": 1, "gatewaySubnet": "192.168.50.1/24", "primary": True,
             "dhcpSettingsVO": {"enable": True, "hostIP": "192.168.50.1", "dhcpPoolMask": 24, "ipaddrStart": "192.168.50.100",
                                "ipaddrEnd": "192.168.50.199"}},
            {"id": "n-smart", "name": "Smart", "purpose": 0, "vlan": 30, "dhcpSettingsVO": {"enable": True, "hostIP": "192.168.30.1",
             "dhcpPoolMask": 24, "ipaddrStart": "192.168.30.50", "ipaddrEnd": "192.168.30.150"}},
            {"id": "n-visit", "name": "Visitors", "purpose": 0, "vlan": 70, "dhcpSettingsVO": {"enable": True, "hostIP": "192.168.70.1",
             "dhcpPoolMask": 24, "ipaddrStart": "192.168.70.50", "ipaddrEnd": "192.168.70.150"}}]
    profiles = [{"id": "pf-all", "name": "All", "nativeNetworkId": "n-home"}, {"id": "pf-smart", "name": "Smart ports", "nativeNetworkId": "n-smart"}]
    ssids = [{"id": "w-home", "name": "Home", "band": 3, "broadcast": True, "security": 3, "vlanEnable": False, "wlan": "Default"},
             {"id": "w-smart", "name": "Smart", "band": 1, "broadcast": True, "security": 3, "vlanEnable": True, "vlanId": 30, "wlan": "Default"},
             {"id": "w-visit", "name": "Visitors", "band": 3, "broadcast": True, "security": 0, "guestNetEnable": True,
              "vlanEnable": True, "vlanId": 70, "wlan": "Default"}]
    acl_gw = [{"id": "g1", "index": 1, "description": "Smart devices stay out of Home", "policy": 0, "status": True, "sourceType": 0,
               "sourceIds": ["n-smart"], "destinationType": 0, "destinationIds": ["n-home"], "protocols": [256]},
              {"id": "g2", "index": 2, "description": "Visitors stay out of Home", "policy": 0, "status": True, "sourceType": 0,
               "sourceIds": ["n-visit"], "destinationType": 0, "destinationIds": ["n-home"], "protocols": [256]},
              {"id": "g3", "index": 3, "description": "Old test rule", "policy": 1, "status": False, "sourceType": 0,
               "sourceIds": ["n-home"], "destinationType": 0, "destinationIds": ["n-smart"], "protocols": [256]}]
    if changed:
        acl_gw.append({"id": "g4", "index": 4, "description": "Visitors may use the printer", "policy": 1, "status": True, "sourceType": 0,
                       "sourceIds": ["n-visit"], "destinationType": 1, "destinationIds": ["ipg-printer"], "protocols": [256]})
    ports = [{"port": i, "name": "Port %d" % i, "status": 1, "profileId": "pf-smart" if i == 7 else "pf-all",
              "profileName": "Smart ports" if i == 7 else "All", "poeMode": 1 if i in (2, 3, 7) else 0} for i in range(1, 9)]
    linked = {2: 3, 3: 3, 4: 3, 5: 3, 6: 2, 7: 2}
    stat = [{"port": i, "name": "Port %d" % i, "type": 1, "disable": False,
             "portStatus": {"linkStatus": 1 if i in linked and not (changed and i == 3) else 0, "linkSpeed": linked.get(i, 0), "poe": i in (2, 3, 7)},
             **({"downlink": {"mac": ap1, "name": "Sample AP upstairs", "type": "ap"}} if i == 2 else {}),
             **({"downlink": {"mac": ap2, "name": "Sample AP garden", "type": "ap"}} if i == 3 else {})} for i in range(1, 9)]
    return {"info": {"controllerVer": "5.15.0"}, "site": {"siteId": "demo", "name": "Default"},
            "devices": devices, "clients": clients, "networks": nets, "profiles": profiles, "ssids": ssids,
            "acl_gateway": acl_gw, "acl_switch": [], "acl_eap": [],
            "wans": [{"name": "WAN1", "port": 1, "mode": 0, "proto": "pppoe", "internetState": 1, "status": 1, "latency": 14, "loss": 0.0,
                      "gateway": gw}],
            "switch_info": {sw: {"portList": ports}},
            "switch_stat": {sw: {"ports": stat, "supportPoe": True, "uplink": {"mac": gw, "port": 5}}}}


def controller_sample(family: str, changed: bool = False) -> dict:
    """Neutral state of the sample access-point controller, made by the family's own translation module."""
    from . import integrations
    st = integrations.load_model(family).to_neutral(_ctl_raw(changed))
    st["controller"]["label"] = "Sample wireless controller"
    st["controller"]["site"] = None
    neutral.validate(st)
    return st


def controller_capabilities(family: str) -> dict:
    return {"family": family, "api": {"kind": "official-local", "version": "demo"},
            "read": ["inventory", "clients", "topology", "networks", "acl", "ports", "port_profiles", "ssids", "wans", "dhcp_reservations", "stats"],
            "write": {}, "rollback": {"device_native": False, "object_snapshot": True}, "limits": {"objects_per_plan": 10},
            "lockout": ["mgmt_network", "gateway_uplink", "self_path", "uplink_ports"]}


# ---------------------------------------------------------------------------- sample home router
def _home_raw(changed: bool = False) -> dict:
    """Collected replies of a sample home router as its family's read half hands them to the translation module: the box
    with a repeater on its 5 GHz radio, a NAS and a printer on LAN ports, a tablet and a phone on wireless. ``changed`` is
    what "happened tonight" in the demo report: the repeater got a new firmware and a visitor's phone joined."""
    def mac(n):
        return "02:4C:4C:00:01:%02X" % n

    box, rep, nas, prn, tab, phn, vis = (mac(n) for n in (1, 2, 0x10, 0x11, 0x20, 0x21, 0x22))

    def iface(uid, name, typ, m, links=()):
        return {"uid": uid, "name": name, "type": typ, "mac_address": m, "node_links": list(links)}

    def link(uid, typ, a, b, ia, ib):
        return {"uid": uid, "type": typ, "state": "CONNECTED", "node_1_uid": a, "node_2_uid": b,
                "node_interface_1_uid": ia, "node_interface_2_uid": ib}

    def node(uid, name, m, ip, role="unknown", model="", fw="", ifaces=()):
        return {"uid": uid, "device_name": name, "device_model": model, "device_firmware_version": fw, "device_mac_address": m,
                "ip_address": ip, "is_meshed": role != "unknown", "mesh_role": role, "node_interfaces": list(ifaces)}

    nodes = [node("n-box", "home.box", box, "192.168.178.1", "master", "Sample home router", "7.57", [
                 iface("b-l2", "LAN:2", "LAN", box, [link("l1", "LAN", "n-box", "n-nas", "b-l2", "nas-i")]),
                 iface("b-l3", "LAN:3", "LAN", box, [link("l2", "LAN", "n-box", "n-prn", "b-l3", "prn-i")]),
                 iface("b-5g", "AP:5G:0", "WLAN", box, [link("l3", "WLAN", "n-box", "n-rep", "b-5g", "rep-s")]),
                 iface("b-2g", "AP:2G:0", "WLAN", box, [link("l4", "WLAN", "n-box", "n-tab", "b-2g", "tab-i")])]),
             node("n-rep", "Sample repeater", rep, "192.168.178.2", "slave", "Sample repeater", "7.58" if changed else "7.57", [
                 iface("rep-s", "STA:5G:0", "WLAN", rep),
                 iface("rep-2g", "AP:2G:0", "WLAN", rep, [link("l5", "WLAN", "n-rep", "n-phn", "rep-2g", "phn-i")])]),
             node("n-nas", "NAS", nas, "192.168.178.20", ifaces=[iface("nas-i", "LAN:1", "LAN", nas)]),
             node("n-prn", "Office printer", prn, "192.168.178.31", ifaces=[iface("prn-i", "LAN:1", "LAN", prn)]),
             node("n-tab", "Kitchen tablet", tab, "192.168.178.52", ifaces=[iface("tab-i", "STA:2G:0", "WLAN", tab)]),
             node("n-phn", "Phone", phn, "192.168.178.53", ifaces=[iface("phn-i", "STA:2G:0", "WLAN", phn)])]
    hosts = [{"ip": "192.168.178.2", "mac": rep, "active": True, "name": "Sample repeater", "interface": "802.11", "port": 0},
             {"ip": "192.168.178.20", "mac": nas, "active": True, "name": "NAS", "interface": "Ethernet", "port": 2},
             {"ip": "192.168.178.31", "mac": prn, "active": True, "name": "Office printer", "interface": "Ethernet", "port": 3},
             {"ip": "192.168.178.52", "mac": tab, "active": True, "name": "Kitchen tablet", "interface": "802.11", "port": 0},
             {"ip": "192.168.178.53", "mac": phn, "active": True, "name": "Phone", "interface": "802.11", "port": 0}]
    if changed:
        hosts.append({"ip": "192.168.178.54", "mac": vis, "active": True, "name": "Visitor phone", "interface": "802.11", "port": 0})
    return {"info": {"model": "Sample home router", "firmware": "7.57", "uptime_s": 1036800},
            "lan": {"ip": "192.168.178.1", "mask": "255.255.255.0", "dhcp": True, "first": "192.168.178.20", "last": "192.168.178.200"},
            "lan_mac": box, "wans": [{"kind": "ip", "status": "Connected", "uptime_s": 86400}],
            "hosts": hosts, "mesh": {"schema_version": "1.9", "nodes": nodes}}


def home_sample(family: str, changed: bool = False) -> dict:
    """Neutral state of the sample home router, made by the family's own translation module."""
    from . import integrations
    st = integrations.load_model(family).to_neutral(_home_raw(changed))
    st["controller"]["label"] = "Sample home router"
    neutral.validate(st)
    return st


def home_capabilities(family: str) -> dict:
    return {"family": family, "api": {"kind": "official-local", "version": "demo"},
            "read": ["inventory", "clients", "topology", "networks", "wans", "stats"],
            "write": {}, "rollback": {"device_native": False, "object_snapshot": True}, "limits": {"objects_per_plan": 10},
            "lockout": ["mgmt_network", "gateway_uplink", "self_path"]}


# ---------------------------------------------------------------------------- sample switching router
def _ros_raw(changed: bool = False) -> dict:
    """Collected replies of a sample switching router as its family's read half hands them to the translation module: a
    router with a bridge, two VLANs, a line to the internet on port 1, a trunk to a switch on port 2, a NAS on port 3, a
    powered camera on port 4 and two wireless devices. ``changed`` is what "happened tonight" in the demo report: a
    forwarding rule was added, a visitor's phone joined and the router got a newer release."""
    def mac(n):
        return "02:4C:4C:00:02:%02X" % n

    def iface(name, typ, m, running=True, rx=0, tx=0):
        return {"name": name, "type": typ, "mac-address": m, "running": "true" if running else "false", "disabled": "false",
                "rx-byte": str(rx), "tx-byte": str(tx)}

    nas, cam, tab, phn, vis, sw = (mac(n) for n in (0x20, 0x21, 0x30, 0x31, 0x32, 0x10))
    ifaces = [iface("ether1", "ether", mac(1), True, 8_400_000_000, 1_100_000_000),
              iface("ether2", "ether", mac(2), True, 2_300_000_000, 5_900_000_000),
              iface("ether3", "ether", mac(3), True, 900_000_000, 3_100_000_000),
              iface("ether4", "ether", mac(4), True, 41_000_000, 6_200_000_000),
              iface("bridge", "bridge", mac(2)), iface("vlan30", "vlan", mac(2))]
    leases = [{"mac-address": nas, "address": "10.77.0.20", "host-name": "storage", "dynamic": "false", "status": "bound"},
              {"mac-address": cam, "address": "10.77.0.21", "host-name": "yard-camera", "dynamic": "false", "status": "bound"},
              {"mac-address": tab, "address": "10.77.0.101", "host-name": "kitchen-tablet", "dynamic": "true", "status": "bound"},
              {"mac-address": phn, "address": "10.77.0.102", "host-name": "phone", "dynamic": "true", "status": "bound"}]
    hosts = [{"mac-address": nas, "on-interface": "ether3", "local": "false"},
             {"mac-address": cam, "on-interface": "ether4", "local": "false"},
             {"mac-address": sw, "on-interface": "ether2", "local": "false"},
             {"mac-address": tab, "on-interface": "wlan1", "local": "false"},
             {"mac-address": phn, "on-interface": "wlan1", "local": "false"}]
    arp = [{"address": "10.77.0.20", "mac-address": nas, "interface": "bridge", "status": "reachable", "complete": "true"},
           {"address": "10.77.0.21", "mac-address": cam, "interface": "bridge", "status": "reachable", "complete": "true"},
           {"address": "10.77.0.101", "mac-address": tab, "interface": "bridge", "status": "reachable", "complete": "true"},
           {"address": "10.77.0.102", "mac-address": phn, "interface": "bridge", "status": "stale", "complete": "true"}]
    reg = [{"mac-address": tab, "ssid": "Home", "band": "5ghz-ax"}, {"mac-address": phn, "ssid": "Home", "band": "2ghz-n"}]
    if changed:
        leases.append({"mac-address": vis, "address": "10.77.30.101", "host-name": "visitor-phone", "dynamic": "true", "status": "bound"})
        hosts.append({"mac-address": vis, "on-interface": "wlan1", "local": "false"})
        arp.append({"address": "10.77.30.101", "mac-address": vis, "interface": "vlan30", "status": "reachable", "complete": "true"})
        reg.append({"mac-address": vis, "ssid": "Guests", "band": "5ghz-ax"})
    filt = [{".id": "*1", "chain": "input", "action": "accept", "connection-state": "established,related", "comment": "accept established"},
            {".id": "*2", "chain": "input", "action": "drop", "in-interface": "ether1", "comment": "drop everything from the internet side"},
            {".id": "*3", "chain": "forward", "action": "accept", "connection-state": "established,related", "comment": "accept established"},
            {".id": "*4", "chain": "forward", "action": "drop", "connection-state": "invalid", "comment": "drop invalid"},
            {".id": "*5", "chain": "forward", "action": "drop", "in-interface": "vlan30", "out-interface": "bridge", "comment": "guests stay out of the home network"},
            {".id": "*6", "chain": "forward", "action": "accept", "disabled": "true", "comment": "old test rule"}]
    if changed:
        filt.append({".id": "*7", "chain": "forward", "action": "accept", "protocol": "tcp", "dst-port": "25565", "comment": "game server"})
    return {"resource": {"board-name": "Sample switching router", "version": "7.16.2 (stable)" if changed else "7.16.1 (stable)",
                         "uptime": "12d4h31m", "cpu-load": "4", "total-memory": "536870912", "free-memory": "402653184"},
            "identity": {"name": "edge-router"}, "routerboard": {"model": "Sample switching router"},
            "interfaces": ifaces,
            "ethernet": [{"name": "ether1", "speed": "1Gbps"}, {"name": "ether2", "speed": "2.5Gbps"},
                         {"name": "ether3", "speed": "1Gbps"}, {"name": "ether4", "speed": "100Mbps"}],
            "poe": [{"name": "ether4", "poe-out": "auto-on", "poe-out-status": "powered-on"}],
            "bridges": [{"name": "bridge", "vlan-filtering": "true", "pvid": "1"}],
            "bridge_ports": [{"interface": "ether2", "bridge": "bridge", "pvid": "1"},
                             {"interface": "ether3", "bridge": "bridge", "pvid": "1"},
                             {"interface": "ether4", "bridge": "bridge", "pvid": "1"}],
            "bridge_vlans": [{"bridge": "bridge", "vlan-ids": "30", "tagged": "bridge,ether2"}],
            "vlans": [{"name": "vlan30", "vlan-id": "30", "interface": "bridge"}],
            "addresses": [{"address": "10.77.0.1/24", "interface": "bridge", "comment": "Home"},
                          {"address": "10.77.30.1/24", "interface": "vlan30", "comment": "Guests"},
                          {"address": "198.51.100.7/24", "interface": "ether1", "dynamic": "true"}],
            "pools": [{"name": "home", "ranges": "10.77.0.100-10.77.0.200"}, {"name": "guests", "ranges": "10.77.30.100-10.77.30.200"}],
            "dhcp_servers": [{"interface": "bridge", "address-pool": "home"}, {"interface": "vlan30", "address-pool": "guests"}],
            "dhcp_clients": [{"interface": "ether1", "status": "bound"}],
            "routes": [{"dst-address": "0.0.0.0/0", "gateway": "198.51.100.1", "immediate-gw": "198.51.100.1%ether1",
                        "active": "true", "distance": "1"}],
            "leases": leases, "arp": arp, "bridge_hosts": hosts,
            "neighbors": [{"mac-address": sw, "address4": "10.77.0.2", "identity": "Sample switch", "board": "CRS-sample",
                           "version": "7.15 (stable)", "interface": "ether2"}],
            "wifi_reg": reg, "wireless_reg": [], "filter": filt,
            "nat": [{".id": "*8", "chain": "srcnat", "action": "masquerade", "out-interface": "ether1"},
                    {".id": "*9", "chain": "dstnat", "action": "dst-nat", "protocol": "tcp", "dst-port": "8080",
                     "to-addresses": "10.77.0.20", "to-ports": "80", "comment": "web"}]}


def ros_sample(family: str, changed: bool = False) -> dict:
    """Neutral state of the sample switching router, made by the family's own translation module."""
    from . import integrations
    st = integrations.load_model(family).to_neutral(_ros_raw(changed))
    st["controller"]["label"] = "Sample switching router"
    neutral.validate(st)
    return st


def ros_capabilities(family: str) -> dict:
    return {"family": family, "api": {"kind": "official-local", "version": "demo"},
            "read": ["inventory", "clients", "topology", "networks", "ports", "policies", "port_forwards", "dhcp_reservations",
                     "wans", "stats"],
            "write": {}, "rollback": {"device_native": False, "object_snapshot": True}, "limits": {"objects_per_plan": 10},
            "lockout": ["mgmt_network", "gateway_uplink", "self_path", "uplink_ports", "admin_interface"]}


def seed_history(core, key: str, now: float = None) -> None:
    """Gives the demo the history a running installation would have: three hours of hit counters (blocked today, hits of
    the last hour) and a few link drops and error episodes on two sample ports."""
    now = time.time() if now is None else now
    for k in range(36, -1, -1):
        t = now - k * 300
        pols = [{"id": pid, "hits": hits} for pid, hits in _hits_at(t)]
        core.hitlog.feed(key, pols, t)
    ev = core.portwatch.events
    if not ev:
        ev["p_e_sw1_12"] = [[now - 19 * 3600, "flap"], [now - 11 * 3600, "flap"], [now - 2.5 * 3600, "flap"]]
        ev["p_e_sw1_7"] = [[now - 9 * 3600, "err"], [now - 1.2 * 3600, "err"]]


def _hits_at(t: float):
    sample = live_sample(t)
    return [(p["id"], p["hits"]) for p in sample["policies"] if p.get("hits") is not None]
