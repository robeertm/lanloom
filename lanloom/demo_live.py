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
