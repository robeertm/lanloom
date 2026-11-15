"""Replies of a small two-node Proxmox VE cluster, as the API returns them (``data`` already unwrapped).

Sample values only: addresses from 192.168.50.0/24, locally administered hardware addresses (02:4c:4c:...),
invented names. ``NOW`` is the fixed clock of every test that looks at ages (never the wall clock).
"""
import copy

NOW = 1_800_000_000.0                       # fixed clock for ages and findings
DAY = 86400

VERSION = {"version": "8.2.4", "release": "8.2", "repoid": "faa83925"}

CLUSTER = [
    {"type": "cluster", "id": "cluster", "name": "lab", "nodes": 2, "quorate": 1, "version": 4},
    {"type": "node", "id": "node/node-a", "name": "node-a", "ip": "192.168.50.10", "online": 1, "local": 1, "nodeid": 1, "level": ""},
    {"type": "node", "id": "node/node-b", "name": "node-b", "ip": "192.168.50.11", "online": 1, "local": 0, "nodeid": 2, "level": ""},
]

RESOURCES = [
    {"id": "node/node-a", "type": "node", "node": "node-a", "status": "online", "cpu": 0.12, "maxcpu": 8, "mem": 12 * 2**30,
     "maxmem": 32 * 2**30, "uptime": 864000, "level": ""},
    {"id": "node/node-b", "type": "node", "node": "node-b", "status": "online", "cpu": 0.05, "maxcpu": 4, "mem": 5 * 2**30,
     "maxmem": 16 * 2**30, "uptime": 432000, "level": ""},
    {"id": "qemu/100", "type": "qemu", "vmid": 100, "node": "node-a", "name": "web", "status": "running", "cpu": 0.03,
     "maxcpu": 2, "mem": 2 * 2**30, "maxmem": 4 * 2**30, "maxdisk": 32 * 2**30, "uptime": 86400, "template": 0,
     "netin": 1_000_000, "netout": 4_000_000, "tags": "web;prod", "hastate": "started"},
    {"id": "qemu/101", "type": "qemu", "vmid": 101, "node": "node-a", "name": "camera-recorder", "status": "running", "cpu": 0.2,
     "maxcpu": 4, "mem": 3 * 2**30, "maxmem": 8 * 2**30, "maxdisk": 500 * 2**30, "uptime": 3600, "template": 0,
     "netin": 9_000_000, "netout": 500_000},
    {"id": "lxc/200", "type": "lxc", "vmid": 200, "node": "node-b", "name": "dns", "status": "running", "cpu": 0.01,
     "maxcpu": 1, "mem": 128 * 2**20, "maxmem": 512 * 2**20, "maxdisk": 8 * 2**30, "uptime": 432000, "template": 0,
     "netin": 300_000, "netout": 200_000, "pool": "infra"},
    {"id": "lxc/201", "type": "lxc", "vmid": 201, "node": "node-b", "name": "old-test", "status": "stopped", "cpu": 0,
     "maxcpu": 1, "mem": 0, "maxmem": 512 * 2**20, "maxdisk": 4 * 2**30, "uptime": 0, "template": 0},
    {"id": "qemu/9000", "type": "qemu", "vmid": 9000, "node": "node-a", "name": "tpl-base", "status": "stopped", "template": 1,
     "maxcpu": 2, "maxmem": 2 * 2**30, "maxdisk": 16 * 2**30},
    {"id": "storage/node-a/local-lvm", "type": "storage", "node": "node-a", "storage": "local-lvm", "status": "available",
     "disk": 200 * 2**30, "maxdisk": 400 * 2**30, "plugintype": "lvmthin", "shared": 0, "content": "images,rootdir"},
    {"id": "sdn/node-a/lan", "type": "sdn", "node": "node-a", "sdn": "lan", "status": "ok"},
]

NODE_STATUS = {
    "node-a": {"cpu": 0.12, "loadavg": ["0.42", "0.38", "0.30"], "memory": {"total": 32 * 2**30, "used": 12 * 2**30, "free": 20 * 2**30},
               "uptime": 864000, "pveversion": "pve-manager/8.2.4/faa83925c9641325", "kversion": "6.8.12-1-pve #1 SMP",
               "cpuinfo": {"cpus": 8, "cores": 4, "sockets": 1, "model": "Sample CPU"}},
    "node-b": {"cpu": 0.05, "loadavg": ["0.10", "0.08", "0.05"], "memory": {"total": 16 * 2**30, "used": 5 * 2**30, "free": 11 * 2**30},
               "uptime": 432000, "pveversion": "pve-manager/8.2.4/faa83925c9641325", "kversion": "6.8.12-1-pve #1 SMP",
               "cpuinfo": {"cpus": 4, "cores": 4, "sockets": 1, "model": "Sample CPU"}},
}

NODE_NETWORK = {
    "node-a": [
        {"iface": "eno1", "type": "eth", "active": 1, "autostart": 1, "method": "manual", "families": ["inet"]},
        {"iface": "eno2", "type": "eth", "active": 1, "autostart": 1, "method": "manual", "families": ["inet"]},
        {"iface": "bond0", "type": "bond", "active": 1, "autostart": 1, "slaves": "eno1 eno2", "bond_mode": "802.3ad"},
        {"iface": "vmbr0", "type": "bridge", "active": 1, "autostart": 1, "bridge_ports": "bond0", "bridge_vlan_aware": 1,
         "bridge_vids": "2-100", "cidr": "192.168.50.10/24", "address": "192.168.50.10", "netmask": "24", "gateway": "192.168.50.1",
         "comments": "LAN"},
        {"iface": "vmbr1", "type": "bridge", "active": 1, "autostart": 1, "bridge_ports": "", "bridge_vlan_aware": 0},
    ],
    "node-b": [
        {"iface": "enp1s0", "type": "eth", "active": 1, "autostart": 1},
        {"iface": "vmbr0", "type": "bridge", "active": 1, "autostart": 1, "bridge_ports": "enp1s0", "bridge_vlan_aware": 0,
         "cidr": "192.168.50.11/24", "gateway": "192.168.50.1"},
    ],
}

NODE_STORAGE = {
    "node-a": [
        {"storage": "local", "type": "dir", "active": 1, "enabled": 1, "total": 100 * 2**30, "used": 40 * 2**30, "content": "iso,vztmpl,backup", "shared": 0},
        {"storage": "local-lvm", "type": "lvmthin", "active": 1, "enabled": 1, "total": 400 * 2**30, "used": 380 * 2**30, "content": "images,rootdir", "shared": 0},
        {"storage": "backup-share", "type": "nfs", "active": 1, "enabled": 1, "total": 2000 * 2**30, "used": 900 * 2**30, "content": "backup", "shared": 1},
    ],
    "node-b": [
        {"storage": "local", "type": "dir", "active": 1, "enabled": 1, "total": 50 * 2**30, "used": 10 * 2**30, "content": "iso,vztmpl,rootdir", "shared": 0},
        {"storage": "backup-share", "type": "nfs", "active": 1, "enabled": 1, "total": 2000 * 2**30, "used": 900 * 2**30, "content": "backup", "shared": 1},
    ],
}

STORAGE_CFG = [
    {"storage": "local", "type": "dir", "content": "iso,vztmpl,backup", "path": "/var/lib/vz"},
    {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir", "thinpool": "data", "vgname": "pve"},
    {"storage": "backup-share", "type": "nfs", "content": "backup", "shared": 1, "server": "192.168.50.20", "export": "/backup"},
]

GUEST_CONFIG = {
    "qemu/100": {"name": "web", "cores": 2, "memory": "4096", "onboot": 1, "agent": "1",
                 "net0": "virtio=02:4C:4C:00:01:00,bridge=vmbr0,firewall=1,tag=20",
                 "net1": "virtio=02:4C:4C:00:01:01,bridge=vmbr1",
                 "scsi0": "local-lvm:vm-100-disk-0,size=32G", "cipassword": "**********", "sshkeys": "ssh-ed25519%20AAAA",
                 "tags": "web;prod", "digest": "0123456789abcdef"},
    "qemu/101": {"name": "camera-recorder", "cores": 4, "memory": "8192", "agent": "0", "protection": 1,
                 "net0": "e1000=02:4C:4C:00:01:10,bridge=vmbr0,tag=150,rate=12.5",
                 "description": "private note", "digest": "abcdef0123456789"},
    "lxc/200": {"hostname": "dns", "cores": 1, "memory": 512, "onboot": 1,
                "net0": "name=eth0,bridge=vmbr0,firewall=1,hwaddr=02:4C:4C:00:02:00,ip=192.168.50.53/24,gw=192.168.50.1,type=veth",
                "digest": "fedcba9876543210"},
    "lxc/201": {"hostname": "old-test", "cores": 1, "memory": 512,
                "net0": "name=eth0,bridge=vmbr9,hwaddr=02:4C:4C:00:02:01,ip=dhcp,type=veth"},
    "qemu/9000": {"name": "tpl-base", "template": 1, "net0": "virtio=02:4C:4C:00:09:00,bridge=vmbr0"},
}

AGENT = {
    "qemu/100": {"result": [
        {"name": "lo", "hardware-address": "00:00:00:00:00:00", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4", "prefix": 8}]},
        {"name": "eth0", "hardware-address": "02:4c:4c:00:01:00", "ip-addresses": [
            {"ip-address": "192.168.50.80", "ip-address-type": "ipv4", "prefix": 24},
            {"ip-address": "fe80::4c:4cff:fe00:100", "ip-address-type": "ipv6", "prefix": 64}]},
    ]},
}

LXC_IFACES = {
    "lxc/200": [{"name": "lo", "hwaddr": "00:00:00:00:00:00", "inet": "127.0.0.1/8"},
                {"name": "eth0", "hwaddr": "02:4c:4c:00:02:00", "inet": "192.168.50.53/24"}],
}

GUEST_FW = {
    "qemu/100": {"options": {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT", "dhcp": 1, "macfilter": 1, "ipfilter": 0},
                 "rules": [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": "443", "comment": "https"},
                           {"pos": 1, "type": "group", "action": "webservers", "enable": 1},
                           {"pos": 2, "type": "in", "action": "DROP", "enable": 0, "source": "+blocklist"}],
                 "aliases": [{"name": "office", "cidr": "192.168.50.0/24"}], "ipset": []},
    "lxc/200": {"options": {"enable": 0}, "rules": [], "aliases": [], "ipset": []},
}

RRD = {
    "qemu/100": [{"time": NOW - 120 * i, "netin": 1000.0 + i, "netout": 5000.0 + i, "cpu": 0.03, "maxcpu": 2} for i in range(40)],
}

DC_FW = {
    "options": {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT", "ebtables": 1},
    "rules": [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1, "macro": "SSH", "source": "+management", "iface": "vmbr0"},
              {"pos": 1, "type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": "8006", "source": "+management"}],
    "groups": [{"group": "webservers", "comment": "web front ends"}],
    "group_rules": {"webservers": [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1, "proto": "tcp", "dport": "80,443"}]},
    "aliases": [{"name": "gateway", "cidr": "192.168.50.1"}],
    "ipsets": [{"name": "management", "comment": "admin hosts"}, {"name": "blocklist"}],
    "ipset_members": {"management": [{"cidr": "192.168.50.5"}, {"cidr": "192.168.50.6"}],
                      "blocklist": [{"cidr": "203.0.113.0/24"}, {"cidr": "203.0.113.7", "nomatch": 1}]},
}

NODE_FW = {
    "node-a": {"options": {"enable": 1, "log_level_in": "nolog"}, "rules": [{"pos": 0, "type": "in", "action": "ACCEPT", "enable": 1, "macro": "Ping"}]},
    "node-b": {"options": {"enable": 1}, "rules": []},
}

SDN = {
    "zones": [{"zone": "lan", "type": "vlan", "bridge": "vmbr0", "mtu": 1500, "ipam": "pve"}],
    "vnets": [{"vnet": "cams", "zone": "lan", "tag": 150, "alias": "Cameras"}, {"vnet": "iot", "zone": "lan", "tag": 30}],
    "subnets": {"cams": [{"subnet": "lan-192.168.150.0-24", "cidr": "192.168.150.0/24", "gateway": "192.168.150.1", "snat": 0}],
                "iot": []},
}

BACKUP_JOBS = [
    {"id": "backup-0a1b2c3d-0001", "enabled": 1, "schedule": "21:00", "storage": "backup-share", "mode": "snapshot",
     "all": 1, "exclude": "201", "next-run": NOW + 6 * 3600, "comment": "nightly"},
    {"id": "backup-0a1b2c3d-0002", "enabled": 0, "schedule": "sun 03:00", "storage": "local", "mode": "stop", "vmid": "101"},
]

NOT_BACKED_UP = [{"vmid": 201, "name": "old-test", "type": "lxc"}]

BACKUP_CONTENT = {
    ("node-a", "local"): [{"volid": "local:backup/vzdump-qemu-101-old.vma.zst", "vmid": 101, "ctime": NOW - 20 * DAY, "size": 9 * 2**30}],
    ("node-a", "backup-share"): [
        {"volid": "backup-share:backup/vzdump-qemu-100-a.vma.zst", "vmid": 100, "ctime": NOW - 1 * DAY, "size": 3 * 2**30,
         "verification": {"state": "ok", "upid": "UPID:x"}},
        {"volid": "backup-share:backup/vzdump-qemu-100-b.vma.zst", "vmid": 100, "ctime": NOW - 2 * DAY, "size": 3 * 2**30},
        {"volid": "backup-share:backup/vzdump-lxc-200-a.tar.zst", "vmid": 200, "ctime": NOW - 1 * DAY, "size": 2**28},
    ],
}

TASKS = {
    "node-a": [{"upid": "UPID:node-a:1", "type": "vzdump", "id": "", "starttime": NOW - DAY - 600, "endtime": NOW - DAY, "status": "OK", "node": "node-a"},
               {"upid": "UPID:node-a:2", "type": "vzdump", "id": "101", "starttime": NOW - 7200, "endtime": NOW - 7000,
                "status": "job errors", "node": "node-a"}],
    "node-b": [],
}

PERMISSIONS_READ = {"/": {"Sys.Audit": 1, "VM.Audit": 1, "Datastore.Audit": 1, "SDN.Audit": 1, "Pool.Audit": 1, "Mapping.Audit": 1}}
PERMISSIONS_WRITE = {"/": {"Sys.Audit": 1, "VM.Audit": 1}, "/vms": {"VM.PowerMgmt": 1, "VM.Audit": 1}}


def raw():
    """The raw dict the read half collects in one full poll (what ``model.to_neutral`` gets)."""
    nodes = {}
    for n in ("node-a", "node-b"):
        nodes[n] = {"status": NODE_STATUS[n], "network": NODE_NETWORK[n], "storage": NODE_STORAGE[n],
                    "fw_options": NODE_FW[n]["options"], "fw_rules": NODE_FW[n]["rules"], "tasks": TASKS[n]}
    guests = {}
    for key, cfg in GUEST_CONFIG.items():
        node = next(r["node"] for r in RESOURCES if r.get("id") == key)
        g = {"node": node, "config": cfg}
        fw = GUEST_FW.get(key)
        if fw:
            g.update({"fw_options": fw["options"], "fw_rules": fw["rules"], "fw_aliases": fw["aliases"], "fw_ipsets": fw["ipset"]})
        if key in AGENT:
            g["agent"] = AGENT[key]
        if key in LXC_IFACES:
            g["ifaces"] = LXC_IFACES[key]
        if key in RRD:
            g["rrd"] = RRD[key]
        guests[key] = g
    backups = []
    seen = set()
    for (node, st), vols in BACKUP_CONTENT.items():
        if st in seen:
            continue
        seen.add(st)
        backups += [dict(v, storage=st) for v in vols]
    return copy.deepcopy({"api_host": "192.168.50.10", "version": VERSION, "cluster": CLUSTER, "resources": RESOURCES,
                          "nodes": nodes, "guests": guests, "dc_fw": DC_FW, "sdn": SDN, "storage_cfg": STORAGE_CFG,
                          "backup_jobs": BACKUP_JOBS, "not_backed_up": NOT_BACKED_UP, "backups": backups,
                          "traffic": {"qemu/100": [1200, 4800]}})
