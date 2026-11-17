"""Translation module of the Proxmox VE family: host/cluster objects -> neutral model v2 (and, later, back).

Pure functions, no I/O. Shared by the read half and (from stage B on) the write half, so app and executor compute
the same neutral state. ``now`` is always passed in (tests use a fixed clock).

Input is a dict of raw replies (whatever the read half could get; every key is optional)::

    version        /version                          resources     /cluster/resources
    cluster        /cluster/status                   storage_cfg   /storage
    nodes          {node: {status, network, storage, fw_options, fw_rules, tasks}}
    guests         {"qemu/100": {config, fw_options, fw_rules, fw_aliases, fw_ipsets, agent, ifaces, rrd}}
    dc_fw          {options, rules, groups, group_rules {name: [...]}, aliases, ipsets, ipset_members {name: [...]}}
    sdn            {zones, vnets, subnets {vnet: [...]}}
    backup_jobs    /cluster/backup                   not_backed_up /cluster/backup-info/not-backed-up
    backups        [{volid, vmid, ctime, size, storage, verification}]   newest backup volumes (metadata only)
    traffic        {"qemu/100": [in bytes/s, out bytes/s]}   rates between two light polls (read half)

Never copied: passwords, keys and secrets of guests (cloud-init ``cipassword``, ``sshkeys``), storage credentials,
descriptions/notes, serial numbers. Hardware addresses of guest network cards leave this module only as a hash
(``mac_hash``); the topology observation (core-internal, never sent to the browser) carries them to match devices.
Neutral ids are opaque hashes; the guest id is made from the cluster-wide guest number, so it survives a migration.
"""
import hashlib
import ipaddress
import re

from ...sources._ctlmap import norm_mac

SALT = "lanloom-proxmox:"
MAX_ITEMS = 5000
TEXT_MAX = 96
STORAGE_WARN = 0.90
STORAGE_ALERT = 0.95
BACKUP_OLD_S = 7 * 86400          # a guest whose newest backup is older than this gets a finding
BACKUP_FAILED_WINDOW_S = 48 * 3600
RRD_POINTS = 30                   # last-hour traffic: at most this many points per guest

ACTIONS = {"ACCEPT": "allow", "DROP": "block", "REJECT": "reject"}
DIRECTIONS = ("in", "out", "group", "forward")
QEMU_MODELS = ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci", "ne2k_isa", "pcnet", "i82551",
               "i82557b", "i82559er")
IFACE_KINDS = {"bridge": "bridge", "bond": "bond", "vlan": "vlan", "eth": "eth", "OVSBridge": "ovs_bridge",
               "OVSBond": "ovs_bond", "OVSPort": "ovs_port", "OVSIntPort": "ovs_port", "alias": "alias"}
STATUSES = ("running", "stopped", "paused", "suspended", "prelaunch")
OWN_MARK = "lanloom:"             # comment prefix of guest firewall rules Lanloom created (only those may be removed)
RULE_PROTOS = ("tcp", "udp", "icmp")


# ---------------------------------------------------------------------------- small helpers
def oid(prefix: str, vendor_id) -> str:
    """Opaque, stable neutral id for a vendor object (never a hardware address itself)."""
    return prefix + hashlib.sha256((SALT + prefix + str(vendor_id)).encode("utf-8")).hexdigest()[:12]


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value) -> list:
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        value = value["data"]
    return [v for v in value[:MAX_ITEMS] if isinstance(v, dict)] if isinstance(value, list) else []


def text(value, limit: int = TEXT_MAX):
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    out = "".join(ch for ch in str(value) if ch >= " ")[:limit].strip()
    return out or None


def num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value if value == value else None
    try:
        f = float(str(value).strip())
    except ValueError:
        return None
    if f != f:
        return None
    return int(f) if f.is_integer() else round(f, 4)


def flag(value) -> bool:
    return num(value) == 1 or value is True


def _ip(value):
    try:
        return str(ipaddress.ip_address(str(value).strip().split("/")[0]))
    except ValueError:
        return None


def _cidr(value):
    try:
        return str(ipaddress.ip_interface(str(value).strip()))
    except ValueError:
        return None


def mac_hash(mac):
    m = norm_mac(mac)
    return oid("m", m) if m else None


def version_tuple(version) -> tuple:
    parts = re.findall(r"\d+", str(version or ""))[:3]
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))


def parse_kv(value) -> dict:
    """``virtio=BC:24:11:00:00:01,bridge=vmbr0,tag=20`` -> dict; a bare first item becomes ``_``."""
    out = {}
    if not isinstance(value, str):
        return out
    for i, part in enumerate(value.split(",")[:32]):
        k, sep, v = part.partition("=")
        k = k.strip()
        if not sep:
            if i == 0 and k:
                out["_"] = k
            continue
        if k:
            out[k] = v.strip()
    return out


def vlan_list(value) -> list:
    """``10;20;30-32`` -> [10, 20, 30, 31, 32] (at most 4094 entries)."""
    out = []
    for part in re.split(r"[;,\s]+", str(value or "")):
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = num(a), num(b) if b else num(a)
        if isinstance(lo, int) and isinstance(hi, int) and 1 <= lo <= hi <= 4094:
            out.extend(range(lo, hi + 1))
        if len(out) >= 4094:
            break
    return sorted(set(out))[:4094]


def tags_of(value) -> list:
    return [t for t in (text(x, 32) for x in re.split(r"[;,\s]+", str(value or ""))) if t][:16]


def guest_key(kind: str, vmid) -> str:
    return "%s/%s" % ("qemu" if kind in ("qemu", "vm") else "lxc", vmid)


# ---------------------------------------------------------------------------- nodes
def _cluster(raw) -> tuple:
    """(cluster info or None, {node name: {ip, online, local}})."""
    info, nodes = None, {}
    for e in _l(raw.get("cluster")):
        if e.get("type") == "cluster":
            info = {"name": text(e.get("name"), 64), "nodes": num(e.get("nodes")), "quorate": flag(e.get("quorate"))}
        elif e.get("type") == "node" and text(e.get("name"), 64):
            nodes[text(e["name"], 64)] = {"ip": _ip(e.get("ip")), "online": flag(e.get("online")), "local": flag(e.get("local"))}
    return info, nodes


def _hosts(raw, cstat) -> list:
    res_nodes = {text(r.get("node"), 64): r for r in _l(raw.get("resources")) if r.get("type") == "node" and text(r.get("node"), 64)}
    names = sorted(set(res_nodes) | set(cstat) | {text(n, 64) for n in _d(raw.get("nodes")) if text(n, 64)})
    api_ip = _ip(raw.get("api_host"))
    out = []
    for name in names:
        r = res_nodes.get(name, {})
        st = _d(_d(_d(raw.get("nodes")).get(name)).get("status"))
        cs = cstat.get(name, {})
        mem = _d(st.get("memory"))
        pvever = text(st.get("pveversion"), 64) or ""
        online = cs.get("online") if name in cstat else (str(r.get("status") or "") == "online" if r else bool(st))
        load = [num(x) for x in (st.get("loadavg") or [])[:3]] if isinstance(st.get("loadavg"), list) else []
        cpuinfo = _d(st.get("cpuinfo"))
        ip = cs.get("ip") or (api_ip if cs.get("local") or len(names) == 1 else None)
        out.append({"id": oid("h", name), "name": name, "online": bool(online), "ip": ip, "local": bool(cs.get("local")),
                    "version": (pvever.split("/")[1] if pvever.count("/") >= 1 else None) or None,
                    "kernel": text(st.get("kversion"), 64) or text(_d(st.get("current-kernel")).get("release"), 64),
                    "cpu": num(st.get("cpu")) if st.get("cpu") is not None else num(r.get("cpu")),
                    "cores": num(cpuinfo.get("cpus")) or num(r.get("maxcpu")),
                    "mem_used": num(mem.get("used")) if mem else num(r.get("mem")),
                    "mem_total": num(mem.get("total")) if mem else num(r.get("maxmem")),
                    "load": [x for x in load if x is not None],
                    "uptime_s": num(st.get("uptime")) if st.get("uptime") is not None else num(r.get("uptime"))})
    return out


# ---------------------------------------------------------------------------- guests and network cards
def _guest_resources(raw) -> dict:
    out = {}
    for r in _l(raw.get("resources")):
        if r.get("type") in ("qemu", "lxc") and isinstance(num(r.get("vmid")), int):
            out[guest_key(r["type"], num(r["vmid"]))] = r
    if isinstance(raw.get("resources"), list):          # the cluster list is the truth (a deleted guest is gone)
        return out
    for key, g in _d(raw.get("guests")).items():         # no cluster list at all: the guests whose config was read
        if re.fullmatch(r"(qemu|lxc)/\d{1,9}", str(key)) and key not in out:
            out[key] = {"type": key.split("/")[0], "vmid": int(key.split("/")[1]), "node": _d(g).get("node")}
    return out


def _nic(kind: str, gid: str, slot: str, value, vnets: set) -> dict:
    kv = parse_kv(value)
    mac, model = None, None
    if kind == "qemu":
        if "_" in kv and kv["_"] in QEMU_MODELS:
            model = kv["_"]
        for m in QEMU_MODELS:
            if m in kv:
                model, mac = m, kv[m]
        model = kv.get("model") or model
        mac = kv.get("macaddr") or mac
    else:
        model, mac = kv.get("type") or "veth", kv.get("hwaddr")
    tag = num(kv.get("tag"))
    rate = num(kv.get("rate"))
    bridge = text(kv.get("bridge"), 32)
    ip = None
    if kind == "lxc" and kv.get("ip") not in (None, "", "dhcp", "manual"):
        ip = _ip(kv.get("ip"))
    return {"id": oid("nic", "%s/%s" % (gid, slot)), "guest": gid, "name": slot, "ifname": text(kv.get("name"), 16),
            "model": text(model, 16), "bridge": bridge, "vnet": bool(bridge and bridge in vnets),
            "vlan": tag if isinstance(tag, int) and 1 <= tag <= 4094 else None, "trunks": vlan_list(kv.get("trunks")),
            "firewall": flag(kv.get("firewall")), "rate_mbs": rate, "link_down": flag(kv.get("link_down")),
            "mtu": num(kv.get("mtu")), "ip": ip, "dhcp": kind == "lxc" and kv.get("ip") == "dhcp",
            "mac_hash": mac_hash(mac), "_mac": norm_mac(mac)}


def _agent_addresses(agent) -> dict:
    """Guest agent reply -> {mac: [ips]} (IPv4 first, loopback and link-local skipped)."""
    out = {}
    res = _d(agent).get("result") if isinstance(agent, dict) else agent
    for itf in res if isinstance(res, list) else []:
        if not isinstance(itf, dict):
            continue
        mac = norm_mac(itf.get("hardware-address"))
        ips = []
        for a in itf.get("ip-addresses") or []:
            ip = _ip(_d(a).get("ip-address"))
            if ip and not ipaddress.ip_address(ip).is_loopback and not ipaddress.ip_address(ip).is_link_local:
                ips.append(ip)
        if mac and ips:
            out[mac] = sorted(ips, key=lambda x: (":" in x, x))[:8]
    return out


def _lxc_addresses(ifaces) -> dict:
    out = {}
    for itf in _l(ifaces):
        mac = norm_mac(itf.get("hwaddr") or itf.get("hardware-address"))
        ips = [ip for ip in (_ip(itf.get("inet")), _ip(itf.get("inet6"))) if ip and not ipaddress.ip_address(ip).is_link_local
               and not ipaddress.ip_address(ip).is_loopback]
        if mac and ips:
            out[mac] = ips
    return out


def _rrd(points) -> dict:
    pts = [p for p in _l(points) if isinstance(num(p.get("time")), (int, float))]
    pts.sort(key=lambda p: num(p["time"]))
    pts = pts[-RRD_POINTS:]
    if not pts:
        return None
    return {"t": [int(num(p["time"])) for p in pts],
            "in": [num(p.get("netin")) for p in pts], "out": [num(p.get("netout")) for p in pts],
            "cpu": [num(p.get("cpu")) for p in pts]}


def _guests(raw, host_ids, vnets) -> tuple:
    """(guests, guest_nics, guest_rules, fw_options of guests, raw objects of guests, {guest id: [(mac, ip)]})."""
    res = _guest_resources(raw)
    details = _d(raw.get("guests"))
    traffic = _d(raw.get("traffic"))
    guests, nics, rules, opts, extra, macs = [], [], [], [], [], {}
    for key in sorted(res, key=lambda k: (int(k.split("/")[1]), k)):
        r = res[key]
        kind, vmid = key.split("/")
        det = _d(details.get(key))
        cfg = _d(det.get("config"))
        gid = oid("g", key)
        node = text(r.get("node") or det.get("node"), 64)
        g_nics = []
        for slot in sorted((k for k in cfg if re.fullmatch(r"net\d{1,2}", str(k))), key=lambda s: int(s[3:])):
            n = _nic(kind, gid, slot, cfg[slot], vnets)
            g_nics.append(n)
        addrs = _agent_addresses(det.get("agent")) if kind == "qemu" else _lxc_addresses(det.get("ifaces"))
        ips = []
        for n in g_nics:
            found = addrs.get(n["_mac"]) or []
            if n["ip"] and n["ip"] not in found:
                found = [n["ip"]] + found
            n["ips"] = found[:8]
            ips += found
            macs.setdefault(gid, []).append((n["_mac"], found[0] if found else None, n["bridge"], n["vlan"]))
        nics += g_nics
        fwo = _d(det.get("fw_options"))
        fw_read = isinstance(det.get("fw_options"), dict)     # an empty answer means every option is at its default
        g_rules = _rules(det.get("fw_rules"), "guest", gid)
        # compact signatures for the night report, which keeps guests but not the card and rule lists:
        # "net0 vmbr0·30, net1 vmbr1" and a short hash of the guest's rules
        net_sig = ", ".join("%s %s%s" % (n["name"], n["bridge"] or "-", "·%d" % n["vlan"] if n["vlan"] else "") for n in g_nics)[:160]
        rules_sig = hashlib.sha256(repr([(r["direction"], r["action"], r["enabled"], r["proto"], r["dport"], r["source"], r["macro"],
                                          r["group"]) for r in g_rules]).encode("utf-8")).hexdigest()[:12] if g_rules else ""
        status = str(r.get("status") or "")
        if status == "running" and str(r.get("lock") or "") == "suspended":
            status = "suspended"
        rate = traffic.get(key) if isinstance(traffic.get(key), list) and len(traffic.get(key)) == 2 else None
        guests.append({"id": gid, "host": host_ids.get(node), "node": node, "kind": "vm" if kind == "qemu" else "lxc",
                       "vmid": int(vmid), "name": text(r.get("name") or cfg.get("name") or cfg.get("hostname"), 64) or "%s %s" % (kind, vmid),
                       "status": status if status in STATUSES else "unknown", "template": flag(r.get("template") or cfg.get("template")),
                       "cpu": num(r.get("cpu")), "cores": num(r.get("maxcpu")) or num(cfg.get("cores")),
                       "mem": num(r.get("mem")), "mem_max": num(r.get("maxmem")), "disk_max": num(r.get("maxdisk")),
                       "uptime_s": num(r.get("uptime")), "tags": tags_of(r.get("tags") or cfg.get("tags")),
                       "ha": text(r.get("hastate"), 24), "pool": text(r.get("pool"), 64), "onboot": flag(cfg.get("onboot")),
                       "lock": text(r.get("lock") or cfg.get("lock"), 24),
                       "protected": "guest.protection" if flag(cfg.get("protection")) else None,
                       "agent": kind == "qemu" and str(parse_kv(cfg.get("agent")).get("_") or parse_kv(cfg.get("agent")).get("enabled") or "0") == "1",
                       "firewall": flag(fwo.get("enable")) if fw_read else None, "ips": sorted(set(ips), key=lambda x: (":" in x, x))[:8],
                       "traffic": {"in_bps": num(rate[0]), "out_bps": num(rate[1])} if rate else None,
                       "rrd": _rrd(det.get("rrd")), "config_read": bool(cfg),
                       "net_sig": net_sig if cfg else None, "rules_sig": rules_sig if det.get("fw_rules") is not None else None,
                       "rules_n": len(g_rules)})
        if fw_read:
            opts.append(_fw_opts("guest", gid, fwo))
        rules += g_rules
        extra += _aliases(det.get("fw_aliases"), "guest", gid) + _ipsets(det.get("fw_ipsets"), {}, "guest", gid)
    for n in nics:
        n.pop("_mac", None)
    return guests, nics, rules, opts, extra, macs


# ---------------------------------------------------------------------------- firewall
def _fw_opts(scope: str, owner: str, o: dict) -> dict:
    o = _d(o)
    out = {"id": oid("fo", "%s/%s" % (scope, owner)), "scope": scope, "owner": owner,
           "enabled": flag(o.get("enable")) if o.get("enable") is not None else (scope == "node"),
           "policy_in": text(o.get("policy_in"), 8), "policy_out": text(o.get("policy_out"), 8),
           "policy_forward": text(o.get("policy_forward"), 8)}
    if scope == "guest":
        out.update({"macfilter": o.get("macfilter") is None or flag(o.get("macfilter")), "ipfilter": flag(o.get("ipfilter")),
                    "dhcp": o.get("dhcp") is None or flag(o.get("dhcp"))})
    return out


def rule_id(scope: str, owner: str, pos) -> str:
    return oid("r", "%s/%s/%s" % (scope, owner, pos))


def _rule(r: dict, scope: str, owner: str, idx: int) -> dict:
    typ = str(r.get("type") or "in")
    act = str(r.get("action") or "")
    pos = num(r.get("pos"))
    pos = pos if isinstance(pos, int) else idx
    comment = text(r.get("comment"), 96)
    out = {"id": rule_id(scope, owner, pos), "scope": scope, "owner": owner,
           "pos": pos, "direction": typ if typ in DIRECTIONS else "in",
           "action": "group" if typ == "group" else ACTIONS.get(act.upper(), "other"),
           "group": text(act, 32) if typ == "group" else None, "enabled": flag(r.get("enable")),
           "proto": text(r.get("proto"), 16) or "any", "sport": text(r.get("sport"), 64), "dport": text(r.get("dport"), 64),
           "source": text(r.get("source"), 96), "dest": text(r.get("dest"), 96), "macro": text(r.get("macro"), 32),
           "iface": text(r.get("iface"), 16), "comment": comment, "log": text(r.get("log"), 12)}
    if scope == "guest":
        # the fields a plan step may write (stage B): rules with a macro, a group, a destination, a source port, an
        # interface or an unusual protocol are shown, and only switched on or off - never rewritten field by field
        own = bool(comment and comment.startswith(OWN_MARK))
        plain = typ in ("in", "out") and out["action"] in ("allow", "block", "reject") and not out["macro"] and not out["dest"] \
            and not out["sport"] and not out["iface"] and out["proto"] in ("any",) + RULE_PROTOS
        out.update(guest=owner, own=own, plain=plain, name=(comment[len(OWN_MARK):].strip() if own else (comment or ""))[:64],
                   ports=out["dport"] or "")
    return out


def _rules(items, scope: str, owner: str) -> list:
    return [_rule(r, scope, owner, i) for i, r in enumerate(_l(items))]


def _aliases(items, scope: str, owner: str) -> list:
    return [{"id": oid("x", "alias/%s/%s/%s" % (scope, owner, a.get("name"))), "area": "fw_alias", "scope": scope, "owner": owner,
             "title": text(a.get("name"), 64) or "?", "detail": text(a.get("cidr"), 64), "editable": False}
            for a in _l(items) if a.get("name")]


def _ipsets(items, members: dict, scope: str, owner: str) -> list:
    out = []
    for s in _l(items):
        if not s.get("name"):
            continue
        mem = _l(_d(members).get(s["name"]))
        out.append({"id": oid("x", "ipset/%s/%s/%s" % (scope, owner, s["name"])), "area": "fw_ipset", "scope": scope, "owner": owner,
                    "title": text(s["name"], 64) or "?", "count": len(mem) if s["name"] in _d(members) else None,
                    "detail": ", ".join(filter(None, (("!" if flag(m.get("nomatch")) else "") + (text(m.get("cidr"), 64) or "") for m in mem[:6]))) or None,
                    "editable": False})
    return out


def _firewall(raw, hosts) -> tuple:
    """(fw_rules, fw_options, raw objects) of datacenter, security groups and nodes."""
    dc = _d(raw.get("dc_fw"))
    rules = _rules(dc.get("rules"), "dc", "datacenter")
    opts = [_fw_opts("dc", "datacenter", dc.get("options"))] if isinstance(dc.get("options"), dict) else []
    extra = _aliases(dc.get("aliases"), "dc", "datacenter") + _ipsets(dc.get("ipsets"), dc.get("ipset_members"), "dc", "datacenter")
    group_rules = _d(dc.get("group_rules"))
    for g in _l(dc.get("groups")):
        name = text(g.get("group"), 32)
        if not name:
            continue
        rules += _rules(group_rules.get(g["group"]), "group", name)
        extra.append({"id": oid("x", "group/%s" % name), "area": "fw_group", "scope": "dc", "owner": "datacenter", "title": name,
                      "detail": text(g.get("comment"), 96), "count": len(_l(group_rules.get(g["group"]))) if g["group"] in group_rules else None,
                      "editable": False})
    nodes = _d(raw.get("nodes"))
    for h in hosts:
        n = _d(nodes.get(h["name"]))
        if isinstance(n.get("fw_options"), dict):
            opts.append(_fw_opts("node", h["id"], n["fw_options"]))
        rules += _rules(n.get("fw_rules"), "node", h["id"])
    return rules, opts, extra


# ---------------------------------------------------------------------------- network: bridges and software-defined network
def _sdn(raw) -> tuple:
    sdn = _d(raw.get("sdn"))
    zones = []
    for z in _l(sdn.get("zones")):
        name = text(z.get("zone"), 32)
        if name:
            zones.append({"id": oid("sz", name), "name": name, "type": text(z.get("type"), 16), "bridge": text(z.get("bridge"), 32),
                          "mtu": num(z.get("mtu")), "pending": bool(z.get("pending")), "state": text(z.get("state"), 16)})
    subnets = _d(sdn.get("subnets"))
    vnets = []
    for v in _l(sdn.get("vnets")):
        name = text(v.get("vnet"), 32)
        if not name:
            continue
        subs = []
        for s in _l(subnets.get(v["vnet"])):
            cidr = text(s.get("cidr"), 64)
            if not cidr and s.get("subnet"):                          # "zone-10.0.0.0-24"
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,2})\Z", str(s["subnet"]))
                cidr = "%s/%s" % m.groups() if m else None
            subs.append({"cidr": cidr, "gateway": _ip(s.get("gateway")), "snat": flag(s.get("snat")),
                         "dhcp": bool(s.get("dhcp-range"))})
        tag = num(v.get("tag"))
        vnets.append({"id": oid("sv", name), "name": name, "zone": oid("sz", text(v.get("zone"), 32)) if v.get("zone") else None,
                      "zone_name": text(v.get("zone"), 32), "tag": tag if isinstance(tag, int) else None, "alias": text(v.get("alias"), 64),
                      "vlan_aware": flag(v.get("vlanaware")), "subnets": subs[:64], "pending": bool(v.get("pending"))})
    return zones, vnets


def _bridges(raw, hosts, sdn_zones, vnets) -> list:
    nodes = _d(raw.get("nodes"))
    zone_bridge = {z["id"]: z["bridge"] for z in sdn_zones if z.get("bridge")}
    out = []
    for h in hosts:
        for itf in _l(_d(nodes.get(h["name"])).get("network")):
            name = text(itf.get("iface"), 32)
            if not name:
                continue
            kind = IFACE_KINDS.get(str(itf.get("type") or ""), "other")
            ports = itf.get("bridge_ports") or itf.get("ovs_ports") or itf.get("slaves") or ""
            cidr = _cidr(itf.get("cidr")) or (_cidr("%s/%s" % (itf.get("address"), itf.get("netmask"))) if itf.get("address") and itf.get("netmask") else None)
            out.append({"id": oid("b", "%s/%s" % (h["name"], name)), "host": h["id"], "name": name, "kind": kind,
                        "vlan_aware": flag(itf.get("bridge_vlan_aware")),
                        "vids": text(itf.get("bridge_vids"), 64) if flag(itf.get("bridge_vlan_aware")) else None,
                        "ports": [p for p in (text(x, 32) for x in str(ports).split()) if p][:32],
                        "cidr": cidr, "cidr6": _cidr(itf.get("cidr6")), "gateway": _ip(itf.get("gateway")),
                        "active": flag(itf.get("active")), "autostart": flag(itf.get("autostart")),
                        "bond_mode": text(itf.get("bond_mode"), 24), "vlan_id": num(itf.get("vlan-id")),
                        "raw_device": text(itf.get("vlan-raw-device"), 32), "comment": text(itf.get("comments"), 96),
                        "mgmt": bool(cidr and h.get("ip") and cidr.split("/")[0] == h["ip"]),
                        "vnets": sorted(v["name"] for v in vnets if zone_bridge.get(v.get("zone")) == name)})
    return out


# ---------------------------------------------------------------------------- storage and backups
def _storage(raw, hosts) -> list:
    cfg = {text(s.get("storage"), 64): s for s in _l(raw.get("storage_cfg")) if s.get("storage")}
    by_host = {h["name"]: h["id"] for h in hosts}
    seen, out = set(), []
    entries = []
    for node in sorted(_d(raw.get("nodes"))):
        for s in _l(_d(_d(raw["nodes"]).get(node)).get("storage")):
            entries.append((node, s))
    if not entries:
        entries = [(r.get("node"), dict(r, storage=r.get("storage"), total=r.get("maxdisk"), used=r.get("disk"),
                                        active=1 if r.get("status") == "available" else 0, type=r.get("plugintype")))
                   for r in _l(raw.get("resources")) if r.get("type") == "storage"]
    for node, s in entries:
        name = text(s.get("storage"), 64)
        if not name:
            continue
        c = cfg.get(name, {})
        shared = flag(s.get("shared")) or flag(c.get("shared"))
        key = name if shared else "%s/%s" % (node, name)
        if key in seen:
            continue
        seen.add(key)
        used, total = num(s.get("used")), num(s.get("total"))
        content = sorted({x for x in str(s.get("content") or c.get("content") or "").split(",") if x})
        out.append({"id": oid("s", key), "host": None if shared else by_host.get(text(node, 64)), "name": name,
                    "type": text(s.get("type") or c.get("type"), 16), "shared": shared, "active": flag(s.get("active")),
                    "enabled": not flag(c.get("disable")) and (s.get("enabled") is None or flag(s.get("enabled"))),
                    "used": used, "total": total, "fraction": round(used / total, 4) if isinstance(used, (int, float)) and total else None,
                    "content": content[:10]})
    return out


def _backups(raw, guests, now: float) -> tuple:
    """(backup jobs, per guest backup facts {guest id: {...}})."""
    by_vmid = {g["vmid"]: g for g in guests}
    jobs, covered, excluded_all = [], {}, set()
    for j in _l(raw.get("backup_jobs")):
        jid = text(j.get("id"), 64)
        if not jid:
            continue
        sel = "all" if flag(j.get("all")) else "pool" if j.get("pool") else "list"
        listed = [num(x) for x in re.split(r"[,\s]+", str(j.get("vmid") or "")) if num(x) is not None]
        excl = [num(x) for x in re.split(r"[,\s]+", str(j.get("exclude") or "")) if num(x) is not None]
        node = text(j.get("node"), 64)
        members = []
        for g in guests:
            if node and g.get("node") != node:
                continue
            if (sel == "all" and g["vmid"] not in excl) or (sel == "list" and g["vmid"] in listed) or (sel == "pool" and g.get("pool") == j.get("pool")):
                members.append(g["id"])
        if flag(j.get("enabled")) if j.get("enabled") is not None else True:
            for m in members:
                covered.setdefault(m, []).append(jid)
        nxt = num(j.get("next-run"))
        jobs.append({"id": oid("bj", jid), "enabled": flag(j.get("enabled")) if j.get("enabled") is not None else True,
                     "schedule": text(j.get("schedule") or (j.get("starttime") and "%s %s" % (j.get("dow") or "", j.get("starttime"))), 64),
                     "storage": text(j.get("storage"), 64), "mode": text(j.get("mode"), 16), "selection": sel,
                     "pool": text(j.get("pool"), 64), "node": node, "guests": members[:500],
                     "excluded": [by_vmid[v]["id"] for v in excl if v in by_vmid], "next_t": nxt if isinstance(nxt, (int, float)) else None,
                     "comment": text(j.get("comment"), 96)})
    not_backed = {num(e.get("vmid")) for e in _l(raw.get("not_backed_up"))}
    newest = {}
    for b in _l(raw.get("backups")):
        vmid, ctime = num(b.get("vmid")), num(b.get("ctime"))
        if vmid in by_vmid and isinstance(ctime, (int, float)) and (vmid not in newest or ctime > newest[vmid]["ctime"]):
            newest[vmid] = {"ctime": ctime, "size": num(b.get("size")), "storage": text(b.get("storage"), 64),
                            "verified": str(_d(b.get("verification")).get("state") or "") == "ok"}
    failed = {}
    for node, n in sorted(_d(raw.get("nodes")).items()):
        for task in _l(_d(n).get("tasks")):
            if str(task.get("type") or "") != "vzdump":
                continue
            end = num(task.get("endtime"))
            status = str(task.get("status") or "")
            if not isinstance(end, (int, float)) or now - end > BACKUP_FAILED_WINDOW_S or status == "OK" or status.startswith("WARNINGS"):
                continue
            vmid = num(task.get("id"))
            key = vmid if vmid in by_vmid else "*%s" % node
            failed[key] = max(failed.get(key, 0), end)
    facts = {}
    for g in guests:
        nb = newest.get(g["vmid"])
        facts[g["id"]] = {"last_t": nb["ctime"] if nb else None, "age_s": round(now - nb["ctime"]) if nb else None,
                          "size": nb["size"] if nb else None, "storage": nb["storage"] if nb else None,
                          "verified": nb["verified"] if nb else False, "jobs": len(covered.get(g["id"], [])),
                          "in_job": bool(covered.get(g["id"])) and g["vmid"] not in not_backed,
                          "failed_t": failed.get(g["vmid"]) or (failed.get("*%s" % g.get("node")) if covered.get(g["id"]) else None)}
    return jobs, facts


# ---------------------------------------------------------------------------- findings
def _finding(kind: str, level: str, subject: str, **params) -> dict:
    return {"id": oid("f", "%s/%s" % (kind, subject)), "kind": kind, "level": level, "subject": subject,
            "params": {k: v for k, v in params.items() if v is not None}}


def findings(state: dict, now: float) -> list:
    """Plain checks of one host/cluster (no other source needed). Levels: alert > warn > info."""
    out = []
    host_name = {h["id"]: h["name"] for h in state.get("hosts") or []}
    for h in state.get("hosts") or []:
        if not h.get("online"):
            out.append(_finding("node_offline", "alert", h["id"], host=h["name"]))
    for s in state.get("storage") or []:
        f = s.get("fraction")
        if s.get("active") and isinstance(f, (int, float)) and f >= STORAGE_WARN:
            out.append(_finding("storage_full", "alert" if f >= STORAGE_ALERT else "warn", s["id"], storage=s["name"],
                                host=host_name.get(s.get("host")), pct=int(f * 100)))
    bridges = {}
    for b in state.get("bridges") or []:
        bridges[(b["host"], b["name"])] = b
    vnets = {v["name"]: v for v in state.get("sdn_vnets") or []}
    nics_of = {}
    for n in state.get("guest_nics") or []:
        nics_of.setdefault(n["guest"], []).append(n)
    dc_on = next((o["enabled"] for o in state.get("fw_options") or [] if o["scope"] == "dc"), None)
    for g in state.get("guests") or []:
        if g.get("template"):
            continue
        b = g.get("backup") or {}
        if g.get("status") == "running" and not b.get("in_job") and not b.get("last_t"):
            out.append(_finding("backup_none", "warn", g["id"], guest=g["name"], vmid=g["vmid"]))
        elif b.get("last_t") and b.get("age_s") is not None and b["age_s"] > BACKUP_OLD_S and (b.get("in_job") or g.get("status") == "running"):
            out.append(_finding("backup_old", "warn", g["id"], guest=g["name"], vmid=g["vmid"], days=int(b["age_s"] // 86400)))
        if b.get("failed_t"):
            out.append(_finding("backup_failed", "warn", g["id"], guest=g["name"], vmid=g["vmid"], t=int(b["failed_t"])))
        for n in nics_of.get(g["id"], []):
            if not n.get("bridge"):
                continue
            br = bridges.get((g.get("host"), n["bridge"]))
            if br is None and n["bridge"] not in vnets and any(k[0] == g.get("host") for k in bridges):
                out.append(_finding("bridge_missing", "alert", n["id"], guest=g["name"], vmid=g["vmid"], nic=n["name"], bridge=n["bridge"],
                                    host=host_name.get(g.get("host"))))
            elif br is not None and n.get("vlan") and br.get("vlan_aware") and br.get("vids") \
                    and n["vlan"] not in vlan_list(str(br["vids"]).replace(" ", ";")):
                out.append(_finding("vlan_not_allowed", "alert", n["id"], guest=g["name"], vmid=g["vmid"], nic=n["name"],
                                    bridge=n["bridge"], vlan=n["vlan"], host=host_name.get(g.get("host"))))
        if dc_on is False and g.get("firewall") and any(n.get("firewall") for n in nics_of.get(g["id"], [])):
            out.append(_finding("fw_inactive", "info", g["id"], guest=g["name"], vmid=g["vmid"]))
    rank = {"alert": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: (rank[f["level"]], f["kind"], f["subject"]))
    return out[:500]


# ---------------------------------------------------------------------------- the whole state
def version_of(raw) -> str:
    v = _d(raw.get("version"))
    v = _d(v.get("data")) or v
    return text(v.get("version"), 32) or ""


def to_neutral(raw: dict, now: float) -> dict:
    """Neutral model v2 from whatever replies are present. Missing areas stay empty; nothing raises."""
    raw = raw if isinstance(raw, dict) else {}
    cinfo, cstat = _cluster(raw)
    hosts = _hosts(raw, cstat)
    host_ids = {h["name"]: h["id"] for h in hosts}
    sdn_zones, vnets = _sdn(raw)
    guests, nics, g_rules, g_opts, g_extra, _ = _guests(raw, host_ids, {v["name"] for v in vnets})
    jobs, facts = _backups(raw, guests, now)
    for g in guests:
        g["backup"] = facts.get(g["id"])
    for h in hosts:
        mine = [g for g in guests if g["host"] == h["id"] and not g["template"]]
        h["guests_running"] = sum(1 for g in mine if g["status"] == "running")
        h["guests_total"] = len(mine)
    fw_rules, fw_opts, fw_extra = _firewall(raw, hosts)
    state = {"v": 2, "zones": [], "networks": [], "devices": [], "allow": [],
             "hosts": hosts, "guests": guests, "guest_nics": nics, "guest_rules": g_rules,
             "bridges": _bridges(raw, hosts, sdn_zones, vnets), "sdn_zones": sdn_zones, "sdn_vnets": vnets,
             "fw_rules": fw_rules, "fw_options": fw_opts + g_opts, "storage": _storage(raw, hosts), "backup_jobs": jobs,
             "raw": fw_extra + g_extra}
    state["findings"] = findings(state, now)
    state["controller"] = {"version": version_of(raw), "apis": ["token"], "kind": "hypervisor",
                           "cluster": cinfo.get("name") if cinfo else None, "quorate": cinfo.get("quorate") if cinfo else None,
                           "nodes": len(hosts)}
    return state


def topology_event(raw: dict, t: float) -> dict:
    """The topology observation for the core (source ``hypervisor``): every node with its address, every guest
    network card with its hardware address, the node it runs on, bridge and VLAN. Core-internal: the hardware
    addresses are used to match inventory devices and never reach the browser."""
    raw = raw if isinstance(raw, dict) else {}
    cinfo, cstat = _cluster(raw)
    hosts = _hosts(raw, cstat)
    host_ids = {h["name"]: h["id"] for h in hosts}
    _, vnets = _sdn(raw)
    guests, _, _, _, _, macs = _guests(raw, host_ids, {v["name"] for v in vnets})
    out_hosts = [{"name": h["name"], "ip": h["ip"], "online": h["online"]} for h in hosts]
    out_guests = []
    for g in guests:
        for mac, ip, bridge, vlan in macs.get(g["id"], []):
            if mac or ip:
                out_guests.append({"mac": mac, "ip": ip, "name": g["name"], "host": g["node"], "bridge": bridge, "vlan": vlan,
                                   "running": g["status"] == "running", "kind": g["kind"]})
    return {"type": "topology", "source": "hypervisor", "t": t, "hosts": out_hosts[:256], "guests": out_guests[:MAX_ITEMS]}


# ---------------------------------------------------------------------------- VLAN chain across sources
def vlan_chain(state: dict, uplinks: dict, switch_states) -> list:
    """Findings where a guest's VLAN cannot leave its node: the network card is tagged, the bridge has a physical
    port, but the switch port the node hangs on (topology) does not carry that VLAN (port settings read by a
    controller adapter). ``uplinks``: {host id: {"ip": address of the switch, "port": port label}};
    ``switch_states``: neutral states with ``equipment``, ``ports`` and ``networks``. Unknown port settings
    (``tagged`` is None) give no finding - nothing is guessed."""
    out = []
    hosts = {h["id"]: h for h in state.get("hosts") or []}
    bridges = {(b["host"], b["name"]): b for b in state.get("bridges") or []}
    guests = {g["id"]: g for g in state.get("guests") or []}
    for host_id, up in sorted((uplinks or {}).items()):
        if host_id not in hosts or not up or not up.get("ip") or up.get("port") in (None, ""):
            continue
        label = str(up["port"]).strip()
        carried, sw_name = None, None
        for sw in switch_states or []:
            eq = next((e for e in sw.get("equipment") or [] if e.get("mgmt_ip") == up["ip"]), None)
            if not eq:
                continue
            port = next((p for p in sw.get("ports") or [] if p.get("device") == eq["id"]
                         and label in (str(p.get("idx")), str(p.get("name") or ""), "Port %s" % p.get("idx"))), None)
            if not port or port.get("tagged") is None:
                continue
            tagged = port.get("tagged") or []
            if "*" in tagged:
                carried = "*"
            else:
                vlan_of = {n["id"]: n.get("vlan") for n in sw.get("networks") or []}
                carried = {vlan_of.get(t) for t in tagged} | {port.get("native_vlan")}
                carried.discard(None)
            sw_name = eq.get("name")
            break
        if carried is None or carried == "*":
            continue
        for n in state.get("guest_nics") or []:
            g = guests.get(n["guest"])
            br = bridges.get((host_id, n.get("bridge")))
            if not g or g.get("host") != host_id or g.get("template") or not n.get("vlan") or not br or not br.get("ports"):
                continue
            if n["vlan"] not in carried:
                out.append(_finding("vlan_chain", "warn", n["id"], guest=g["name"], vmid=g["vmid"], nic=n["name"], vlan=n["vlan"],
                                    host=hosts[host_id]["name"], switch=sw_name, port=label))
    return out
