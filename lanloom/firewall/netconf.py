"""Gateway settings on the neutral model (stage B): networks and VLANs, fixed addresses (address reservations of the
gateway's address service) and port forwarding. Pure functions, no I/O.

Used twice, with the same rules: by the web app when it builds a preview and by the executor's write half right before
it writes (with its own fresh read). A refusal on either side stops the plan.

What may be written here, and what never:

* **networks** - create an internal network with a VLAN of its own and private addresses (IPv4, /16 to /29, no overlap
  with another network, a VLAN no other network uses), rename it, change its address range or switch its address
  service on and off; delete only a network Lanloom created itself and nothing refers to any more (no device, no fixed
  address, no wireless network, no switch port, no port forward). Never: the management network, internet uplinks
  (WAN), VPN and guest-portal networks, the address range or VLAN of a network that holds devices.
* **fixed addresses** - reserve an address for a known device inside its own network (free, not the gateway), release a
  reservation. Infrastructure Lanloom protects keeps its address.
* **port forwards** - create, change, switch on and off; delete only forwards Lanloom created. The target is a private
  address inside a local network: never a gateway address, never network equipment. Every new forward and every
  change other than its name needs its own confirmation (it opens a device to the internet).
"""
import ipaddress
import secrets

from . import steps as stepkinds

NET_KINDS = ("network.create", "network.update", "network.delete", "dhcp.reserve", "dhcp.release",
             "forward.create", "forward.update", "forward.delete")
PREFIX_MIN, PREFIX_MAX = 16, 29
LOCAL_PURPOSES = ("lan", "iot", "", None)        # networks that take devices; guest portals, uplinks and VPNs do not
VLAN_FIRST = 2
FORWARD_DEFAULTS = {"proto": "tcp_udp", "dst_port": "", "enabled": True}
# templates of "New network": a name and where the suggestion starts (VLAN = third number of the address where free)
TEMPLATES = (
    {"id": "iot", "name_key": "net.tpl.iot_name", "vlan": 30, "purpose": "iot"},
    {"id": "cameras", "name_key": "net.tpl.cameras_name", "vlan": 70, "purpose": "iot"},
    {"id": "work", "name_key": "net.tpl.work_name", "vlan": 40, "purpose": "lan"},
    {"id": "lan", "name_key": "net.tpl.lan_name", "vlan": 20, "purpose": "lan"},
)
FIELD_ORDER = ("name", "cidr", "vlan", "dhcp", "zone", "purpose", "client", "ip", "proto", "wan_port", "dst_ip", "dst_port",
               "enabled")


def _by_id(state: dict, area: str) -> dict:
    return {o["id"]: o for o in state.get(area) or [] if isinstance(o, dict) and "id" in o}


def _net(cidr):
    try:
        return ipaddress.ip_network(cidr or "", strict=False)
    except (TypeError, ValueError):
        return None


def _addr(ip):
    try:
        return ipaddress.ip_address(ip or "")
    except (TypeError, ValueError):
        return None


def gateway_of(net: dict):
    """The gateway address of a network: the one the family reports, else the first host address."""
    if net.get("gateway_ip"):
        return net["gateway_ip"]
    n = _net(net.get("cidr"))
    if n is None or n.num_addresses < 4:
        return None
    return str(n.network_address + 1)


def is_local(net: dict) -> bool:
    return (net.get("purpose") in LOCAL_PURPOSES) and bool(net.get("cidr"))


def default_zone(state: dict):
    """The zone a new network lands in when no zone is chosen: the family's internal zone, else the zone of the main
    network (the local network with the most devices). None when the family has no zones."""
    zones = state.get("zones") or []
    for z in zones:
        if str(z.get("system") or "").lower() == "internal":
            return z["id"]
    for z in zones:
        if str(z.get("id") or "").lower().endswith("internal") or str(z.get("name") or "").strip().lower() == "internal":
            return z["id"]
    count = {}
    for d in state.get("devices") or []:
        count[d.get("network")] = count.get(d.get("network"), 0) + 1
    nets = [n for n in state.get("networks") or [] if is_local(n) and not n.get("management") and n.get("zone")]
    nets.sort(key=lambda n: -count.get(n["id"], 0))
    return nets[0]["zone"] if nets else None


def used_vlans(state: dict, skip: str = None) -> set:
    return {n.get("vlan") for n in state.get("networks") or [] if n.get("id") != skip and isinstance(n.get("vlan"), int)}


def _overlaps(state: dict, cidr, skip: str = None):
    net = _net(cidr)
    if net is None:
        return None
    for n in state.get("networks") or []:
        other = _net(n.get("cidr"))
        if n.get("id") != skip and other is not None and other.version == net.version and net.overlaps(other):
            return n
    return None


def suggest(state: dict, template: str = "lan") -> dict:
    """A free network for a template: name, private /24, a VLAN no other network uses, address service on (.100 to
    .199 like most home networks), the default zone. Follows the scheme already in use (192.168.<n>.0/24 or
    10.<a>.<n>.0/24) and never overlaps an existing network."""
    tpl = next((t for t in TEMPLATES if t["id"] == template), TEMPLATES[-1])
    used = used_vlans(state)
    first = next((_net(n.get("cidr")) for n in state.get("networks") or [] if is_local(n) and _net(n.get("cidr"))), None)
    if first is not None and first.version == 4 and str(first.network_address).startswith("10."):
        a = int(str(first.network_address).split(".")[1])
        pattern = "10.%d.%%d.0/24" % a
    elif first is not None and first.version == 4 and str(first.network_address).startswith("172."):
        pattern = "172.%d.%%d.0/24" % int(str(first.network_address).split(".")[1])
    else:
        pattern = "192.168.%d.0/24"
    start = tpl["vlan"]
    for k in list(range(start, 250)) + list(range(VLAN_FIRST, start)):
        if k in used:
            continue
        cidr = pattern % k
        if _overlaps(state, cidr) is None:
            base = ipaddress.ip_network(cidr)
            return {"template": tpl["id"], "name_key": tpl["name_key"], "cidr": cidr, "vlan": k, "purpose": tpl["purpose"],
                    "gateway_ip": str(base.network_address + 1),
                    "dhcp": {"on": True, "range": [str(base.network_address + 100), str(base.network_address + 199)]},
                    "zone": default_zone(state)}
    return {"template": tpl["id"], "name_key": tpl["name_key"], "cidr": None, "vlan": None, "purpose": tpl["purpose"],
            "gateway_ip": None, "dhcp": {"on": True, "range": None}, "zone": default_zone(state)}


def references(state: dict, net_id: str) -> dict:
    """What still refers to a network: {"devices", "reservations", "ssids", "ports", "forwards"} (counts)."""
    net = _by_id(state, "networks").get(net_id) or {}
    cidr = _net(net.get("cidr"))
    vlan = net.get("vlan")

    def inside(ip):
        a = _addr(ip)
        return bool(cidr is not None and a is not None and a in cidr)

    out = {"devices": 0, "reservations": 0, "ssids": 0, "ports": 0, "forwards": 0}
    for d in state.get("devices") or []:
        if d.get("network") == net_id:
            out["devices"] += 1
    for r in state.get("dhcp_reservations") or []:
        if r.get("network") == net_id or inside(r.get("ip")):
            out["reservations"] += 1
    for s in state.get("ssids") or []:
        if s.get("network") == net_id:
            out["ssids"] += 1
    if isinstance(vlan, int):
        for p in state.get("ports") or []:
            if p.get("native_vlan") == vlan or vlan in (p.get("tagged") or []) or p.get("native_network") == net_id:
                out["ports"] += 1
    for f in state.get("port_forwards") or []:
        if inside(f.get("dst_ip")):
            out["forwards"] += 1
    return out


def _range_problem(net_like: dict, dhcp: dict):
    """Why an address range does not fit its network, or None."""
    if not isinstance(dhcp, dict) or not dhcp.get("on") or not dhcp.get("range"):
        return None
    cidr = _net(net_like.get("cidr"))
    lo, hi = (_addr(x) for x in dhcp["range"])
    if cidr is None or lo is None or hi is None or lo not in cidr or hi not in cidr or lo > hi:
        return "fw.block.dhcp_range"
    gw = _addr(gateway_of(net_like))
    if lo in (cidr.network_address, cidr.broadcast_address) or hi in (cidr.network_address, cidr.broadcast_address) or \
            (gw is not None and lo <= gw <= hi):
        return "fw.block.dhcp_range"
    return None


def _new_network_problem(state: dict, after: dict, skip: str = None):
    cidr = _net(after.get("cidr"))
    if cidr is None or cidr.version != 4 or not cidr.is_private or not PREFIX_MIN <= cidr.prefixlen <= PREFIX_MAX:
        return "fw.block.net_cidr"
    if str(cidr.network_address) != str(after.get("cidr")).split("/")[0]:
        return "fw.block.net_cidr"                      # the network address itself (192.168.30.0/24, not .5/24)
    if _overlaps(state, after["cidr"], skip=skip) is not None:
        return "fw.block.net_overlap"
    vlan = after.get("vlan")
    if not isinstance(vlan, int) or isinstance(vlan, bool) or not VLAN_FIRST <= vlan <= 4094:
        return "fw.block.net_vlan"
    if vlan in used_vlans(state, skip=skip):
        return "fw.block.net_vlan_taken"
    if after.get("purpose") not in ("lan", "iot", None):
        return "fw.block.network_kind"
    if after.get("zone") is not None and after["zone"] not in _by_id(state, "zones"):
        return "fw.block.unknown"
    return _range_problem(after, after.get("dhcp"))


def _taken(state: dict, ip: str, skip_client: str = None) -> bool:
    for d in state.get("devices") or []:
        if d.get("id") == skip_client:
            continue
        if ip in (d.get("ip"), d.get("fixed_ip")) and (d.get("online", True) or d.get("fixed_ip") == ip):
            return True
    return any(r.get("ip") == ip and r.get("client") != skip_client for r in state.get("dhcp_reservations") or [])


def _ports(spec: str) -> list:
    out = []
    for part in str(spec or "").split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def _protos(p: str) -> set:
    return {"tcp", "udp"} if p in ("tcp_udp", "", None) else {p}


def _forward_problem(state: dict, merged: dict, skip: str = None, critical=()):
    ip = _addr(merged.get("dst_ip"))
    if ip is None or ip.version != 4 or not ip.is_private:
        return "fw.block.forward_target"
    home = None
    for n in state.get("networks") or []:
        c = _net(n.get("cidr"))
        if c is not None and ip in c and is_local(n):
            home = n
            break
    if home is None:
        return "fw.block.forward_target"
    c = _net(home["cidr"])
    if ip in (c.network_address, c.broadcast_address):
        return "fw.block.forward_target"
    gateways = {gateway_of(n) for n in state.get("networks") or []} - {None}
    if str(ip) in gateways:
        return "fw.block.forward_gateway"
    infra = {e.get("mgmt_ip") for e in state.get("equipment") or []} | \
        {d.get("ip") for d in state.get("devices") or [] if d.get("protected") in ("fw.reason.gateway", "fw.reason.infrastructure")}
    if str(ip) in infra:
        return "fw.block.management"                    # never the network equipment itself
    if home.get("management") and home.get("purpose") not in ("lan", None, ""):
        return "fw.block.management"
    if str(ip) in set(critical or ()):
        return "fw.block.forward_gateway"
    wan, dst = merged.get("wan_port") or "", merged.get("dst_port") or ""
    if not wan:
        return "fw.block.forward_ports"
    wr = _ports(wan)
    if (len(wr) > 1 or wr[0][0] != wr[0][1]) and dst not in ("", wan):
        return "fw.block.forward_ports"                # a port list or range is forwarded to the same ports
    if merged.get("enabled", True):
        mine = _protos(merged.get("proto"))
        for f in state.get("port_forwards") or []:
            if f.get("id") == skip or not f.get("enabled", True) or not (_protos(f.get("proto")) & mine):
                continue
            try:
                theirs = _ports(f.get("wan_port"))
            except ValueError:
                continue
            if any(a <= d and c <= b for a, b in wr for c, d in theirs):
                return "fw.block.forward_clash"
    return None


def step_lockout(state: dict, step: dict, critical=()):
    """A refusal key for one gateway step, or None. ``critical``: addresses a port forward must never reach (the
    gateway's own, the controller's)."""
    kind = step.get("kind")
    if kind not in NET_KINDS:
        return None
    after, before = step.get("after") or {}, step.get("before") or {}
    nets = _by_id(state, "networks")
    devs = _by_id(state, "devices")
    if kind == "network.create":
        return _new_network_problem(state, after)
    if kind in ("network.update", "network.delete"):
        net = nets.get(step.get("object"))
        if net is None:
            return "fw.block.unknown"
        if not is_local(net):
            return "fw.block.network_kind"
        if net.get("management"):
            return "fw.block.management"
        if kind == "network.delete":
            if not net.get("own"):
                return "fw.block.not_own"
            if any(references(state, net["id"]).values()):
                return "fw.block.network_in_use"
            return None
        merged = dict(net, **after)
        if "name" in after and not str(after["name"]).strip():
            return "fw.block.net_name"
        if ("cidr" in after or "vlan" in after) and any(references(state, net["id"]).values()):
            return "fw.block.network_in_use"
        if "cidr" in after or "vlan" in after:
            prob = _new_network_problem(state, dict(merged, purpose=merged.get("purpose") if merged.get("purpose") in ("lan", "iot") else None),
                                        skip=net["id"])
            if prob:
                return prob
        if "purpose" in after and after["purpose"] not in ("lan", "iot"):
            return "fw.block.network_kind"
        if "dhcp" in after:
            prob = _range_problem(merged, after["dhcp"])
            if prob:
                return prob
            if not after["dhcp"].get("on") and (net.get("dhcp") or {}).get("on"):
                dynamic = [d for d in devs.values() if d.get("network") == net["id"] and d.get("online", True) and not d.get("fixed_ip")]
                if dynamic:
                    return "fw.block.dhcp_in_use"
        return None
    if kind == "dhcp.reserve":
        dev = devs.get(after.get("client"))
        if dev is None:
            return "fw.block.unknown"
        if dev.get("protected"):
            return "fw.block.protected"
        if any(r.get("client") == dev["id"] for r in state.get("dhcp_reservations") or []):
            return "fw.block.reservation_exists"
        net = nets.get(dev.get("network"))
        if net is None:
            return "fw.block.no_network"
        if not is_local(net):
            return "fw.block.network_kind"
        a, c = _addr(after.get("ip")), _net(net.get("cidr"))
        if a is None or c is None or a not in c:
            return "fw.block.ip_outside"
        if a in (c.network_address, c.broadcast_address) or str(a) == gateway_of(net):
            return "fw.block.ip_reserved"
        if _taken(state, str(a), skip_client=dev["id"]):
            return "fw.block.ip_taken"
        return None
    if kind == "dhcp.release":
        res = _by_id(state, "dhcp_reservations").get(step.get("object"))
        if res is None:
            return "fw.block.unknown"
        dev = devs.get(res.get("client")) or {}
        if dev.get("protected"):
            return "fw.block.protected"
        return None
    # port forwards
    if kind == "forward.create":
        return _forward_problem(state, dict(FORWARD_DEFAULTS, **after), critical=critical)
    fwd = _by_id(state, "port_forwards").get(step.get("object"))
    if fwd is None:
        return "fw.block.unknown"
    if kind == "forward.delete":
        return None if fwd.get("own") else "fw.block.not_own"
    merged = dict(fwd, **after)
    if set(after) <= {"name"} or (set(after) <= {"name", "enabled"} and not merged.get("enabled")):
        return None                                     # renaming or switching off never opens anything
    return _forward_problem(state, merged, skip=fwd["id"], critical=critical)


def plan_lockout(state: dict, steps: list):
    """Rules over the whole plan: two new networks must not share addresses or a VLAN, two reservations not an address,
    two forwards not a port."""
    nets, vlans, ips, ports = [], set(), set(), []
    for st in steps:
        kind, after = st.get("kind"), st.get("after") or {}
        if kind == "network.create":
            c = _net(after.get("cidr"))
            if c is not None and any(c.overlaps(o) for o in nets):
                return "fw.block.net_overlap"
            if c is not None:
                nets.append(c)
            if after.get("vlan") in vlans:
                return "fw.block.net_vlan_taken"
            vlans.add(after.get("vlan"))
        elif kind == "dhcp.reserve":
            if after.get("ip") in ips:
                return "fw.block.ip_taken"
            ips.add(after.get("ip"))
        elif kind in ("forward.create", "forward.update") and "wan_port" in after:
            fwd = _by_id(state, "port_forwards").get(st.get("object")) or {}
            merged = dict(FORWARD_DEFAULTS, **dict(fwd, **after))
            if not merged.get("enabled", True):
                continue
            try:
                mine = (_protos(merged.get("proto")), _ports(merged.get("wan_port")))
            except ValueError:
                continue
            for protos, rng in ports:
                if protos & mine[0] and any(a <= d and c <= b for a, b in rng for c, d in mine[1]):
                    return "fw.block.forward_clash"
            ports.append(mine)
    return None


# ---------------------------------------------------------------------------- drafts and preview (web app)
def op_key(kind: str, obj: str) -> str:
    return "%s|%s" % (kind, obj)


def _valid(field_check, value) -> bool:
    try:
        field_check(value)
        return True
    except stepkinds.FieldError:
        return False


def _cur(obj: dict, field: str, sample=None):
    v = obj.get(field)
    if field == "dhcp" and isinstance(v, dict):
        rng = v.get("range")
        return {"on": bool(v.get("on")), "range": list(rng) if isinstance(rng, (list, tuple)) and len(rng) == 2 and all(rng) else None}
    return "" if v is None and isinstance(sample, str) else v


def _norm(field: str, v):
    if isinstance(v, str):
        v = v.strip()
    if field == "dhcp" and isinstance(v, dict):
        rng = v.get("range")
        v = {"on": bool(v.get("on")), "range": [str(x).strip() for x in rng] if isinstance(rng, (list, tuple)) and len(rng) == 2 and all(rng) else None}
    if field == "vlan" and isinstance(v, str) and v.isdigit():
        v = int(v)
    return v


def clean_ops(state: dict, ops) -> dict:
    """Draft gateway changes {key: {"kind", "object", "after"}} reduced to valid, real changes, each with ``before``
    read from ``state``. A new object gets a fresh ``new:`` id; a removal carries the whole object as ``before``."""
    out = {}
    items = ops.values() if isinstance(ops, dict) else ops if isinstance(ops, list) else []
    for op in items:
        if not isinstance(op, dict) or op.get("kind") not in NET_KINDS or not isinstance(op.get("after", {}), dict):
            continue
        kind = stepkinds.KINDS[op["kind"]]
        after = {f: _norm(f, v) for f, v in (op.get("after") or {}).items() if f in kind.fields}
        obj_id = op.get("object")
        if kind.verb == "create":
            if not isinstance(obj_id, str) or not stepkinds.NEW_ID.match(obj_id):
                obj_id = "new:" + secrets.token_hex(8)
            if kind.name == "network.create":
                after.setdefault("purpose", "lan")
                after.setdefault("dhcp", {"on": True, "range": None})
                if after.get("zone") in (None, ""):
                    zone = default_zone(state)
                    after.pop("zone", None)
                    if zone:
                        after["zone"] = zone
                if after["dhcp"].get("on") and not after["dhcp"].get("range"):
                    c = _net(after.get("cidr"))
                    if c is not None and c.num_addresses >= 8:
                        hosts = c.num_addresses - 2
                        lo = c.network_address + (100 if hosts > 200 else max(2, hosts // 4))
                        hi = c.network_address + (199 if hosts > 200 else hosts)
                        after["dhcp"] = {"on": True, "range": [str(lo), str(hi)]}
            elif kind.name == "forward.create":
                after = dict(FORWARD_DEFAULTS, **after)
            step = {"kind": kind.name, "object": obj_id, "before": {}, "after": after}
        elif kind.verb == "delete":
            obj = _by_id(state, kind.area).get(obj_id)
            if not obj:
                continue
            before = {}
            for f, check in kind.fields.items():
                v = _cur(obj, f, "")
                if v not in (None,) and _valid(check, v):
                    before[f] = v
            if not before:
                continue
            step = {"kind": kind.name, "object": obj_id, "before": before, "after": {}}
        else:
            obj = _by_id(state, kind.area).get(obj_id)
            if not obj:
                continue
            keep = {f: v for f, v in after.items() if v != _cur(obj, f, v)}
            if not keep:
                continue
            step = {"kind": kind.name, "object": obj_id, "before": {f: _cur(obj, f, v) for f, v in keep.items()}, "after": keep}
        try:
            stepkinds.check_step(step)
        except stepkinds.FieldError:
            continue
        out[op_key(kind.name, obj_id)] = step
    return out


def label(state: dict, step: dict) -> str:
    """Display name of the object a gateway step acts on."""
    kind = step["kind"]
    after, before = step.get("after") or {}, step.get("before") or {}
    if kind.startswith("dhcp."):
        client = after.get("client") or before.get("client")
        res = _by_id(state, "dhcp_reservations").get(step.get("object")) or {}
        return (_by_id(state, "devices").get(client) or {}).get("name") or res.get("name") or "?"
    area = stepkinds.KINDS[kind].area
    obj = _by_id(state, area).get(step.get("object")) or {}
    return obj.get("name") or after.get("name") or before.get("name") or "?"


WHAT = {"network.create": "net.change.new", "network.update": "net.change.update", "network.delete": "net.change.gone",
        "dhcp.reserve": "net.change.reserve", "dhcp.release": "net.change.release", "forward.create": "net.change.fwd_new",
        "forward.update": "net.change.fwd_update", "forward.delete": "net.change.fwd_gone"}


def preview(state: dict, ops: dict, *, deps: dict = None, critical=(), lockout=None) -> dict:
    """Steps, plain-language changes, warnings (with the confirmation token of confirmed steps), blocks and
    confirmations for drafted gateway changes. ``ops``: the result of ``clean_ops``; ``deps``: {device id: [{"by",
    "via"}]} - what uses a device by its address; ``lockout``: an extra family rule (step -> key or None)."""
    from . import model
    steps, changes, warnings, blocks, confirm = [], [], [], [], []
    deps = deps or {}
    devs = _by_id(state, "devices")
    nets = _by_id(state, "networks")
    for key in sorted(ops):
        st = ops[key]
        name = label(state, st)
        kind = st["kind"]
        why = model.lockout_reason(state, st) if stepkinds.KINDS[kind].verb != "create" else None
        why = why or step_lockout(state, st, critical) or (lockout(st) if lockout else None)
        if why:
            blocks.append([why, {"name": name, "reason": ""}])
            continue
        steps.append(st)
        order = lambda kv: (FIELD_ORDER.index(kv[0]) if kv[0] in FIELD_ORDER else 99, kv[0])
        if stepkinds.KINDS[kind].verb == "create":
            edit = [{"field": f, "from": "", "to": v} for f, v in sorted(st["after"].items(), key=order)
                    if v not in ("", None) and f not in ("client", "zone", "purpose")]
        elif stepkinds.KINDS[kind].verb == "delete":
            edit = [{"field": f, "from": v, "to": ""} for f, v in sorted(st["before"].items(), key=order)
                    if v not in ("", None) and f not in ("client", "zone", "purpose")]
        else:
            edit = [{"field": f, "from": st["before"].get(f), "to": v} for f, v in sorted(st["after"].items(), key=order)]
        changes.append({"device": st["object"], "name": name, "kind": kind, "edit": edit, "what": WHAT[kind]})
        token = stepkinds.confirm_token(st) if stepkinds.needs_confirmation(st) else None
        if token:
            confirm.append(token)
        after = st["after"]
        if kind == "network.create":
            zone = (_by_id(state, "zones").get(after.get("zone")) or {}).get("name")
            warnings.append(["fw.warn.net_new", {"name": name, "cidr": after.get("cidr"), "vlan": after.get("vlan")}])
            if zone:
                warnings.append(["fw.warn.net_zone", {"name": name, "zone": zone}])
        elif kind == "network.update":
            net = nets.get(st["object"]) or {}
            if "dhcp" in after:
                if not after["dhcp"].get("on"):
                    warnings.append(["fw.warn.dhcp_off", {"name": name}])
                elif after["dhcp"].get("range"):
                    lo, hi = (_addr(x) for x in after["dhcp"]["range"])
                    out = [d for d in devs.values() if d.get("network") == net.get("id") and not d.get("fixed_ip")
                           and _addr(d.get("ip")) is not None and not (lo <= _addr(d.get("ip")) <= hi)]
                    if out:
                        warnings.append(["fw.warn.dhcp_range", {"name": name, "n": len(out)}])
            if "name" in after:
                warnings.append(["fw.warn.net_rename", {"name": net.get("name") or name, "new": after["name"]}])
        elif kind == "network.delete":
            warnings.append(["fw.warn.net_delete", {"name": name}])
        elif kind == "dhcp.reserve":
            warnings.append(["fw.warn.fixed_ip_lease", {"name": name, "ip": after.get("ip")}])
        elif kind == "dhcp.release":
            client = st["before"].get("client")
            warnings.append(["fw.warn.fixed_ip_removed", {"name": name}])
            for dep in deps.get(client, []):
                warnings.append(dep_warning(name, dep))
        elif kind in ("forward.create", "forward.update"):
            fwd = _by_id(state, "port_forwards").get(st["object"]) or {}
            merged = dict(FORWARD_DEFAULTS, **dict(fwd, **after))
            if token and merged.get("enabled"):
                warnings.append(["fw.warn.forward_open", {"name": name, "port": merged.get("wan_port") or "?",
                                                          "ip": merged.get("dst_ip") or "?", "confirm": token}])
            elif token:
                warnings.append(["fw.warn.forward_change", {"name": name, "confirm": token}])
        elif kind == "forward.delete":
            warnings.append(["fw.warn.forward_gone", {"name": name}])
    why = plan_lockout(state, steps)
    if why:
        blocks.append([why, {}])
    return {"steps": steps, "changes": changes, "warnings": warnings, "blocks": blocks, "confirm": confirm}


VIA = ("integration", "tracker", "automation", "lanloom")


def dep_warning(name: str, dep: dict) -> list:
    """The warning for something that uses a device by its address (hub integration, presence tracker, automation,
    a source of Lanloom itself); a dependency without a kind keeps the plain text of earlier releases."""
    via = dep.get("via")
    if via in VIA:
        return ["fw.warn.addr_" + via, {"name": name, "by": str(dep.get("by") or "?")[:80]}]
    return ["fw.warn.dependency", {"name": name, "by": str(dep.get("by") or "?")[:80]}]
