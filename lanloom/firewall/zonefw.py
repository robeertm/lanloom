"""Zone firewall on the neutral model (stage B): zones of their own, which zone a network belongs to, rules between
zones (policies) and their order - and the answer to "may A reach B?" before and after a draft. Pure functions, no I/O.

Used twice, with the same rules: by the web app when it builds a preview and by the executor right before it writes
(with its own fresh read). A refusal on either side stops the plan.

What may be written here, and what never:

* **zones** - create a zone of its own, rename it, delete it only when Lanloom created it and nothing is in it any more
  (no network, no rule: controllers delete the rules of a removed zone without a word). Built-in zones (internal,
  external, gateway, VPN, ...) are never renamed or deleted.
* **networks in zones** - move a local network into a zone of its own or back into the internal zone. Never the
  management network, never into a system zone other than the internal one.
* **rules** - create, change, switch off and on; delete only rules Lanloom created; change the order of the rules of
  one zone pair (one move per pair and plan). Never a blocking rule whose source or target is the management network,
  the management zone or the gateway (it would cut off address service, name service and the controller itself).
* **reachability** - after the plan, every path that must stay open still does: the device of whoever approves the
  plan, the executor and Lanloom itself must still reach the controller and the gateway. A rule that would cut one of
  these paths is refused, with the path named.

"May A reach B?" (``verdict``) evaluates the rules in order, first match wins, like the controllers do; traffic within
one zone is allowed unless a rule says otherwise; a pair no rule decides gets the family's default (block). Rules that
narrow by something Lanloom cannot evaluate (addresses lists, regions, apps, domains, schedules) make the answer
"depends", with the rules named. States without rules (the v1 model) answer from the allowed zone pairs.
"""
import copy
import ipaddress
import secrets

from . import steps as stepkinds

ZONE_KINDS = ("zone.create", "zone.update", "zone.delete", "policy.create", "policy.update", "policy.delete",
              "policy.reorder")
POLICY_DEFAULTS = {"proto": "any", "ports": "", "return_traffic": False, "enabled": True}
SYSTEM_TARGETS = ("internal",)          # the only system zone a network may be moved into
WHAT = {"zone.create": "zf.change.zone_new", "zone.update": "zf.change.zone_update", "zone.delete": "zf.change.zone_gone",
        "policy.create": "zf.change.pol_new", "policy.update": "zf.change.pol_update", "policy.delete": "zf.change.pol_gone",
        "policy.reorder": "zf.change.pol_order"}
FIELD_ORDER = ("name", "action", "src", "dst", "proto", "ports", "return_traffic", "enabled", "order")
MAX_REACH_PAIRS = 400


# ---------------------------------------------------------------------------- small helpers
def _by_id(state: dict, area: str) -> dict:
    return {o["id"]: o for o in state.get(area) or [] if isinstance(o, dict) and "id" in o}


def _addr(ip):
    try:
        return ipaddress.ip_address(str(ip or ""))
    except ValueError:
        return None


def _net(cidr):
    try:
        return ipaddress.ip_network(str(cidr or ""), strict=False)
    except ValueError:
        return None


def system_zone(state: dict, kind: str):
    """The id of a system zone (``internal``, ``external``, ``gateway``, ``vpn``, ...) or None."""
    for z in state.get("zones") or []:
        if str(z.get("system") or "").lower() == kind:
            return z["id"]
    return None


def management_zones(state: dict) -> set:
    return {n.get("zone") for n in state.get("networks") or [] if n.get("management") and n.get("zone")}


def protected_zones(state: dict) -> set:
    """Zones a blocking rule must never name as source or target: management and the gateway itself."""
    out = management_zones(state)
    gw = system_zone(state, "gateway")
    if gw:
        out.add(gw)
    return out


def management_networks(state: dict) -> set:
    return {n["id"] for n in state.get("networks") or [] if n.get("management")}


def policies(state: dict) -> list:
    return sorted((p for p in state.get("policies") or [] if isinstance(p, dict)),
                  key=lambda p: (p.get("order", 0) if isinstance(p.get("order"), (int, float)) else 0, str(p.get("id"))))


def pair_of(p: dict) -> tuple:
    return ((p.get("src") or {}).get("zone"), (p.get("dst") or {}).get("zone"))


def has_policies(state: dict) -> bool:
    return bool(state.get("policies"))


def _gateway_of(net: dict):
    if net.get("gateway_ip"):
        return net["gateway_ip"]
    n = _net(net.get("cidr"))
    if n is None or n.num_addresses < 4:
        return None
    return str(n.network_address + 1)


# ---------------------------------------------------------------------------- endpoints of a reachability question
def resolve(state: dict, spec):
    """An endpoint ``{"zone", "net", "ip", "label", "kind"}`` from ``device:<id>``, ``net:<id>``, ``zone:<id>``,
    ``internet``, ``gateway`` or ``ip:<address>``; None when it does not exist."""
    spec = str(spec or "")
    nets = _by_id(state, "networks")
    zones = _by_id(state, "zones")
    kind, _, ref = spec.partition(":")
    if kind == "device":
        dev = _by_id(state, "devices").get(ref)
        if not dev:
            return None
        net = nets.get(dev.get("network")) or {}
        return {"kind": "device", "zone": net.get("zone"), "net": net.get("id"), "ip": dev.get("ip") or None,
                "label": dev.get("name") or "?"}
    if kind == "net":
        net = nets.get(ref)
        return {"kind": "net", "zone": net.get("zone"), "net": net["id"], "ip": None, "label": net.get("name") or "?"} if net else None
    if kind == "zone":
        z = zones.get(ref)
        return {"kind": "zone", "zone": z["id"], "net": None, "ip": None, "label": z.get("name") or "?"} if z else None
    if spec == "internet":
        return {"kind": "internet", "zone": system_zone(state, "external"), "net": None, "ip": None, "label": "internet"}
    if spec == "gateway":
        return {"kind": "gateway", "zone": system_zone(state, "gateway"), "net": None, "ip": None, "label": "gateway"}
    if kind == "ip":
        return of_address(state, ref)
    return None


def of_address(state: dict, ip: str):
    """The endpoint of an address: the gateway zone for an address of the gateway itself, else the network it lies
    in; outside every network the internet. None for a malformed address."""
    a = _addr(ip)
    if a is None:
        return None
    nets = [n for n in state.get("networks") or [] if _net(n.get("cidr")) is not None]
    gw_zone = system_zone(state, "gateway")
    for n in nets:
        if str(a) == _gateway_of(n):
            if gw_zone:
                return {"kind": "gateway", "zone": gw_zone, "net": None, "ip": str(a), "label": str(a)}
            return {"kind": "gateway", "zone": n.get("zone"), "net": n["id"], "ip": str(a), "label": str(a)}
    for n in nets:
        c = _net(n.get("cidr"))
        if c.version == a.version and a in c:
            return {"kind": "ip", "zone": n.get("zone"), "net": n["id"], "ip": str(a), "label": str(a)}
    if a.is_private:
        return None
    return {"kind": "internet", "zone": system_zone(state, "external"), "net": None, "ip": str(a), "label": str(a)}


# ---------------------------------------------------------------------------- evaluation
def _ports_list(spec: str):
    out = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def _match_ports(spec, port):
    """yes / no / maybe for a ports text of a rule against a port of the question (None = any port)."""
    spec = str(spec or "")
    if not spec:
        return "yes"
    if port is None:
        return "maybe"
    neg = spec.startswith("!")
    try:
        rng = _ports_list(spec.lstrip("!"))
    except ValueError:
        return "maybe"                                   # a named list Lanloom cannot see
    hit = any(lo <= port <= hi for lo, hi in rng)
    return "yes" if hit != neg else "no"


def _match_proto(p_proto, q_proto):
    p_proto = str(p_proto or "any")
    if p_proto in ("any", ""):
        return "yes"
    neg = p_proto.startswith("!")
    base = p_proto.lstrip("!")
    if q_proto in (None, "any"):
        return "maybe"
    if base.startswith("#"):
        return "maybe"
    hit = q_proto == base or (base == "tcp_udp" and q_proto in ("tcp", "udp"))
    return "yes" if hit != neg else "no"


def _match_side(side: dict, ep: dict):
    side = side or {}
    zone = side.get("zone")
    if zone not in (ep.get("zone"), "*"):
        return "no"
    out = "yes"
    if side.get("net"):
        if ep.get("net") is None:
            out = "maybe"                                # a whole zone asked, the rule names one network of it
        elif side["net"] != ep["net"]:
            return "no"
    m = side.get("match")
    if m:
        if m.get("kind") == "network" and m.get("items"):
            if ep.get("net") is None:
                out = "maybe"
            else:
                hit = ep["net"] in m["items"]
                if hit == bool(m.get("opposite")):
                    return "no"
        elif m.get("kind") == "ip" and ep.get("ip") and not m.get("group"):
            hit = False
            for item in m.get("items") or []:
                try:
                    if "-" in item:
                        lo, hi = (ipaddress.ip_address(x) for x in item.split("-", 1))
                        hit = hit or lo <= ipaddress.ip_address(ep["ip"]) <= hi
                    else:
                        hit = hit or ipaddress.ip_address(ep["ip"]) in ipaddress.ip_network(item, strict=False)
                except ValueError:
                    return "maybe"
            if hit == bool(m.get("opposite")):
                return "no"
        else:
            out = "maybe"                                # clients, regions, apps, domains, address lists
    if side.get("ports"):
        out = "maybe"                                    # a source port: unknown for a question
    return out


def _match(p: dict, src: dict, dst: dict, proto, port):
    if not p.get("enabled", True):
        return "no"
    if p.get("ip_version") == "v6" and (src.get("ip") or dst.get("ip") or "").count(":") == 0:
        return "no"
    states = [str(s).lower() for s in p.get("states") or []]
    if states and "new" not in states:
        return "no"                                      # answers and established connections only
    parts = [_match_side(p.get("src"), src), _match_side(p.get("dst"), dst), _match_proto(p.get("proto"), proto),
             _match_ports(p.get("ports"), port)]
    if "no" in parts:
        return "no"
    if p.get("scheduled"):
        return "maybe"
    return "maybe" if "maybe" in parts else "yes"


def _v1_allows(state: dict, src_zone, dst_zone) -> bool:
    if src_zone == dst_zone:
        return True
    for a, b in state.get("allow") or []:
        if a in (src_zone, "*") and b in (dst_zone, "*"):
            return True
    return False


def verdict(state: dict, src: dict, dst: dict, proto: str = None, port: int = None) -> dict:
    """``{"verdict": "allow" | "block" | "depends" | "unknown", "by": {"id", "name", "action"} | None,
    "default": key | None, "depends": [{"id", "name", "action"}]}``. ``src``/``dst`` from ``resolve``."""
    out = {"verdict": "unknown", "by": None, "default": None, "depends": []}
    if not src or not dst:
        return out
    zones = _by_id(state, "zones")
    if dst.get("kind") == "internet" and not dst.get("zone"):
        z = zones.get(src.get("zone")) or {}
        out.update(verdict="allow" if z.get("internet") else "block", default="zf.default.internet_flag")
        return out
    if dst.get("kind") == "gateway" and not dst.get("zone"):
        out.update(verdict="allow", default="zf.default.gateway")
        return out
    if not src.get("zone") or not dst.get("zone"):
        return out
    if not has_policies(state):
        ok = _v1_allows(state, src["zone"], dst["zone"])
        out.update(verdict="allow" if ok else "block", default="zf.default.same_zone" if src["zone"] == dst["zone"] else "zf.default.pairs")
        return out
    for p in policies(state):
        m = _match(p, src, dst, proto, port)
        if m == "no":
            continue
        ref = {"id": p.get("id"), "name": p.get("name") or "?", "action": "allow" if p.get("action") == "allow" else "block"}
        if m == "maybe":
            if len(out["depends"]) < 8:
                out["depends"].append(ref)
            continue
        out["by"] = ref
        out["verdict"] = ref["action"]
        break
    else:
        if src["zone"] == dst["zone"]:
            out.update(verdict="allow", default="zf.default.same_zone")
        else:
            out.update(verdict=state.get("policy_default") if state.get("policy_default") in ("allow", "block") else "block",
                       default="zf.default.no_rule")
    if out["depends"]:
        base = out["verdict"]
        if any(d["action"] != base for d in out["depends"]):
            out["verdict"] = "depends"
            out["otherwise"] = base
        else:
            out["depends"] = []
    return out


def ask(state: dict, src_spec: str, dst_spec: str, proto: str = None, port: int = None) -> dict:
    src, dst = resolve(state, src_spec), resolve(state, dst_spec)
    v = verdict(state, src, dst, proto, port)
    v["src"] = {k: src.get(k) for k in ("kind", "label", "zone", "net")} if src else None
    v["dst"] = {k: dst.get(k) for k in ("kind", "label", "zone", "net")} if dst else None
    return v


def zone_matrix(state: dict) -> dict:
    """{(from zone, to zone): verdict} for every pair of different zones (whole zones, any traffic)."""
    out = {}
    zs = [z["id"] for z in state.get("zones") or []][:20]
    for a in zs:
        for b in zs:
            if a != b:
                out[(a, b)] = verdict(state, {"kind": "zone", "zone": a, "net": None, "ip": None},
                                      {"kind": "zone", "zone": b, "net": None, "ip": None})["verdict"]
    return out


# ---------------------------------------------------------------------------- the state after a draft
def reorder(state: dict, pid: str, target) -> dict:
    """{policy id: new order} for the rules of one zone pair when rule ``pid`` moves to the slot ``target`` (one of the
    order numbers the pair's own rules hold now); empty when it cannot. Built-in rules keep their place."""
    pols = _by_id(state, "policies")
    p = pols.get(pid)
    if not p or p.get("builtin") or not isinstance(target, int):
        return {}
    pair = pair_of(p)
    group = [q for q in policies(state) if pair_of(q) == pair and not q.get("builtin")]
    slots = [q.get("order", 0) for q in group]
    if target not in slots or target == p.get("order"):
        return {}
    rest = [q for q in group if q["id"] != pid]
    if target < p.get("order", 0):
        pos = sum(1 for q in rest if q.get("order", 0) < target)
    else:
        pos = sum(1 for q in rest if q.get("order", 0) <= target)
    rest.insert(pos, p)
    return {q["id"]: slots[i] for i, q in enumerate(rest)}


def after_state(state: dict, steps: list) -> dict:
    """The state after the steps, for the questions "may A reach B?" and the lockout check (zones, rules, which zone
    a network is in, new networks, moved devices). Other steps leave it as it is."""
    new = copy.deepcopy(state)
    zones = new.setdefault("zones", [])
    pols = new.setdefault("policies", []) if state.get("policies") is not None or any(
        str(s.get("kind", "")).startswith("policy.") for s in steps) else new.get("policies")
    nets = new.setdefault("networks", [])
    devs = {d["id"]: d for d in new.get("devices") or [] if isinstance(d, dict)}
    touched_rules = False
    for st in steps:
        kind, after, obj = st.get("kind"), st.get("after") or {}, stepkinds.step_object(st)
        if kind == "zone.create":
            zones.append(dict({"internet": False, "discovery": False}, **after, id=obj, builtin=False, own=True))
        elif kind == "zone.update":
            for z in zones:
                if z.get("id") == obj:
                    z.update(copy.deepcopy(after))
        elif kind == "zone.delete":
            zones[:] = [z for z in zones if z.get("id") != obj]
            fallback = system_zone(new, "internal")
            for n in nets:
                if n.get("zone") == obj:
                    n["zone"] = fallback
            if pols is not None:
                pols[:] = [p for p in pols if obj not in pair_of(p)]
            touched_rules = True
        elif kind == "policy.create":
            pair = (after.get("src", {}).get("zone"), after.get("dst", {}).get("zone"))
            mine = [p.get("order", 0) for p in pols if pair_of(p) == pair and not p.get("builtin")]
            allorders = [p.get("order", 0) for p in pols if isinstance(p.get("order"), (int, float))]
            order = (max(mine) + 1) if mine else ((max(allorders) + 1) if allorders else 10000)
            pols.append(dict(POLICY_DEFAULTS, **copy.deepcopy(after), id=obj, order=order, own=True, editable=True, builtin=False))
            touched_rules = True
        elif kind == "policy.update":
            for p in pols or []:
                if p.get("id") == obj:
                    p.update(copy.deepcopy(after))
            touched_rules = True
        elif kind == "policy.delete":
            pols[:] = [p for p in pols or [] if p.get("id") != obj]
            touched_rules = True
        elif kind == "policy.reorder":
            moved = reorder(new, obj, after.get("order"))
            for p in pols or []:
                if p.get("id") in moved:
                    p["order"] = moved[p["id"]]
            touched_rules = True
        elif kind == "network.create":
            nets.append({"id": obj, "name": after.get("name"), "cidr": after.get("cidr"), "vlan": after.get("vlan"),
                         "zone": after.get("zone"), "management": False, "purpose": after.get("purpose", "lan")})
        elif kind == "network.update" and "zone" in after:
            for n in nets:
                if n.get("id") == obj:
                    n["zone"] = after["zone"]
        elif kind == "move" and obj in devs:
            devs[obj]["network"], devs[obj]["ip"] = after.get("network"), after.get("ip")
        elif kind == "client.set" and obj in devs and "network" in after:
            devs[obj]["network"] = after["network"]
            if after.get("fixed_ip"):
                devs[obj]["ip"] = after["fixed_ip"]
    if touched_rules and pols is not None:
        refresh_allow(new)
    return new


def refresh_allow(state: dict) -> None:
    """Recompute the v1 ``allow`` pairs (and, where an external zone exists, each zone's internet flag) from the rules,
    so the board and the device abilities follow a rule change."""
    from . import neutral
    allow = neutral.derive_allow(state, default=state.get("policy_default") or "block")
    state["allow"] = allow
    ext = system_zone(state, "external")
    if ext:
        for z in state.get("zones") or []:
            z["internet"] = any(a == z["id"] and b == ext for a, b in allow)


# ---------------------------------------------------------------------------- lockout rules
def _name_taken(state: dict, name: str, skip: str = None) -> bool:
    want = str(name or "").strip().lower()
    return any(str(z.get("name") or "").strip().lower() == want for z in state.get("zones") or [] if z.get("id") != skip)


def zone_refs(state: dict, zone_id: str) -> dict:
    """What still refers to a zone: {"networks", "rules"} (counts)."""
    return {"networks": sum(1 for n in state.get("networks") or [] if n.get("zone") == zone_id),
            "rules": sum(1 for p in state.get("policies") or [] if zone_id in pair_of(p))}


def zone_target_problem(state: dict, zone_id):
    """Why a network cannot be placed into this zone, or None."""
    z = _by_id(state, "zones").get(zone_id)
    if z is None:
        return "fw.block.unknown"
    system = str(z.get("system") or "").lower()
    if system and system not in SYSTEM_TARGETS:
        return "zf.block.zone_system"
    if z.get("builtin") and system not in SYSTEM_TARGETS:
        return "zf.block.zone_system"
    return None


def _endpoint_problem(state: dict, side: dict):
    zones = _by_id(state, "zones")
    nets = _by_id(state, "networks")
    if side.get("zone") not in zones:
        return "fw.block.unknown"
    if side.get("group"):
        return "fw.block.capability"
    if side.get("net"):
        n = nets.get(side["net"])
        if n is None:
            return "fw.block.unknown"
        if n.get("zone") != side["zone"]:
            return "zf.block.pol_net_zone"
    return None


def step_lockout(state: dict, step: dict):
    """A refusal key for one zone or rule step, or None (the neutral rules of ``model.lockout_reason`` run as well)."""
    kind = step.get("kind")
    after = step.get("after") or {}
    zones = _by_id(state, "zones")
    pols = _by_id(state, "policies")
    if kind == "zone.create":
        if not str(after.get("name") or "").strip():
            return "zf.block.zone_name"
        if _name_taken(state, after["name"]):
            return "zf.block.zone_name_taken"
        return None
    if kind in ("zone.update", "zone.delete"):
        z = zones.get(step.get("object"))
        if z is None:
            return "fw.block.unknown"
        if z.get("builtin") or z.get("system"):
            return "zf.block.zone_builtin"
        if kind == "zone.update":
            if "name" in after and (not str(after["name"]).strip()):
                return "zf.block.zone_name"
            if "name" in after and _name_taken(state, after["name"], skip=z["id"]):
                return "zf.block.zone_name_taken"
            return None
        if not z.get("own"):
            return "fw.block.not_own"
        refs = zone_refs(state, z["id"])
        if refs["networks"]:
            return "zf.block.zone_has_networks"
        if refs["rules"]:
            return "zf.block.zone_has_rules"
        return None
    if kind in ("policy.create", "policy.update"):
        p = pols.get(step.get("object")) if kind == "policy.update" else {}
        if kind == "policy.update" and p is None:
            return "fw.block.unknown"
        if kind == "policy.update" and (p.get("builtin") or p.get("editable") is False):
            return "fw.block.builtin"
        merged = dict(POLICY_DEFAULTS, **dict(p or {}, **after))
        for side in ("src", "dst"):
            why = _endpoint_problem(state, merged.get(side) or {})
            if why:
                return why
        if merged.get("ports") and merged.get("proto") not in ("tcp", "udp", "tcp_udp"):
            return "zf.block.pol_ports_proto"
        if merged.get("action") in ("block", "reject"):
            prot = protected_zones(state)
            mnets = management_networks(state)
            for side in ("src", "dst"):
                s = merged.get(side) or {}
                if s.get("zone") in prot or s.get("net") in mnets:
                    return "zf.block.mgmt_rule"
        return None
    if kind == "policy.delete":
        p = pols.get(step.get("object"))
        if p is None:
            return "fw.block.unknown"
        if not p.get("own"):
            return "fw.block.not_own"
        return None
    if kind == "policy.reorder":
        p = pols.get(step.get("object"))
        if p is None:
            return "fw.block.unknown"
        if p.get("builtin") or p.get("editable") is False:
            return "fw.block.builtin"
        if not reorder(state, p["id"], after.get("order")):
            return "zf.block.order_range"
        return None
    if kind in ("network.create", "network.update") and after.get("zone") is not None:
        why = zone_target_problem(state, after["zone"])
        if why:
            return why
        if kind == "network.update":
            net = _by_id(state, "networks").get(step.get("object")) or {}
            if net.get("management"):
                return "fw.block.management"
    return None


def needs_confirmation(state: dict, step: dict) -> bool:
    """A rule that lets something in from the internet, or from another zone into the management zone, is confirmed on
    its own (``confirm:<kind>:<id>``) - the same token as the plain step confirmations."""
    if step.get("kind") not in ("policy.create", "policy.update"):
        return False
    old = _by_id(state, "policies").get(step.get("object")) or {}
    merged = dict(POLICY_DEFAULTS, **dict(old, **(step.get("after") or {})))
    if merged.get("action") != "allow" or not merged.get("enabled", True):
        return False
    if step["kind"] == "policy.update" and old.get("action") == "allow" and old.get("enabled", True) and \
            set(step.get("after") or {}) <= {"name"}:
        return False
    src, dst = (merged.get("src") or {}).get("zone"), (merged.get("dst") or {}).get("zone")
    ext = system_zone(state, "external")
    if ext and src == ext:
        return True
    return dst in management_zones(state) and src != dst


def reach_lockout(before: dict, after: dict, paths: list):
    """(key, vars) for the first path that is open (or open for some traffic) before and closed after, or None.
    ``paths``: [(source endpoint, target endpoint, proto, port, label)]."""
    for src_b, dst_b, proto, port, label in paths:
        vb = verdict(before, src_b, dst_b, proto, port)["verdict"]
        if vb not in ("allow", "depends"):
            continue
        src_a = _same_endpoint(after, src_b)
        dst_a = _same_endpoint(after, dst_b)
        va = verdict(after, src_a, dst_a, proto, port)["verdict"]
        if va == "block":
            return "zf.block.lockout_path", {"from": src_b.get("label") or "?", "to": dst_b.get("label") or "?",
                                             "reason": label or "zf.path.controller"}
    return None


def _same_endpoint(state: dict, ep: dict):
    """The same endpoint in another state (a device or address may sit in another zone after the plan)."""
    if ep is None:
        return None
    if ep.get("device"):
        e = resolve(state, "device:" + ep["device"])
        return dict(e, label=ep.get("label")) if e else None
    if ep.get("ip"):
        e = of_address(state, ep["ip"])
        return dict(e, label=ep.get("label")) if e else None
    if ep.get("net"):
        e = resolve(state, "net:" + ep["net"])
        return dict(e, label=ep.get("label")) if e else None
    return ep


def guard_paths(state: dict, *, requester: str = None, sources=(), targets=()) -> list:
    """Paths that must stay open: from the device of the approver (``requester``, a device id) and from each address
    in ``sources`` (the executor, Lanloom itself) to the gateway and to each ``targets`` entry ((address, port,
    label), e.g. the controller)."""
    froms = []
    if requester:
        dev = resolve(state, "device:" + requester)
        if dev:
            froms.append(dict(dev, device=requester))
    for ip in sources:
        ep = of_address(state, ip)
        if ep and ep.get("kind") != "gateway":
            froms.append(dict(ep, label=ep.get("label") or ip))
    tos = []
    gw = resolve(state, "gateway")
    if gw and gw.get("zone"):
        tos.append((gw, "udp", 53, "zf.path.gateway"))
    for ip, port, label in targets:
        ep = of_address(state, ip)
        if ep:
            tos.append((dict(ep, label=ip), "tcp", port, label or "zf.path.controller"))
    out = []
    for f in froms:
        for t, proto, port, label in tos:
            if f.get("zone") and t.get("zone") and not (f.get("ip") and f.get("ip") == t.get("ip")):
                out.append((f, t, proto, port, label))
    return out


def plan_lockout(state: dict, steps: list, *, requester: str = None, sources=(), targets=()):
    """Rules over the whole plan (refusal key or None): one order move per zone pair and never together with a new or
    removed rule of that pair, no two new zones with the same name, and every guarded path stays open."""
    return plan_lockout_vars(state, steps, requester=requester, sources=sources, targets=targets)[0]


def plan_lockout_vars(state: dict, steps: list, *, requester: str = None, sources=(), targets=()) -> tuple:
    pols = _by_id(state, "policies")
    reorders, changed_pairs, names = set(), set(), set()
    for st in steps:
        kind, after = st.get("kind"), st.get("after") or {}
        if kind == "policy.reorder":
            pair = pair_of(pols.get(st.get("object")) or {})
            if pair in reorders:
                return "zf.block.order_one", {}
            reorders.add(pair)
        elif kind == "policy.create":
            changed_pairs.add(((after.get("src") or {}).get("zone"), (after.get("dst") or {}).get("zone")))
        elif kind == "policy.delete":
            changed_pairs.add(pair_of(pols.get(st.get("object")) or st.get("before") or {}))
        elif kind == "zone.create":
            n = str(after.get("name") or "").strip().lower()
            if n in names:
                return "zf.block.zone_name_taken", {}
            names.add(n)
    if reorders & changed_pairs:
        return "zf.block.order_mix", {}
    relevant = any(str(st.get("kind", "")).startswith(("zone.", "policy.")) or
                   (st.get("kind") == "network.update" and "zone" in (st.get("after") or {})) or
                   st.get("kind") in ("move", "client.set") for st in steps)
    if not relevant:
        return None, {}
    paths = guard_paths(state, requester=requester, sources=sources, targets=targets)
    if not paths:
        return None, {}
    hit = reach_lockout(state, after_state(state, steps), paths)
    if hit:
        return hit
    return None, {}


# ---------------------------------------------------------------------------- drafts and preview (web app)
def op_key(kind: str, obj: str) -> str:
    return "%s|%s" % (kind, obj)


def _norm_side(v):
    if not isinstance(v, dict):
        return v
    return {k: str(v[k]).strip() for k in ("zone", "net", "group") if v.get(k) not in (None, "")}


def _norm(field: str, v):
    if isinstance(v, str):
        v = v.strip()
    if field in ("src", "dst"):
        v = _norm_side(v)
    if field == "ports" and isinstance(v, str):
        v = v.replace(" ", "")
    if field == "order" and isinstance(v, str) and v.isdigit():
        v = int(v)
    return v


def _cur(obj: dict, field: str, sample=None):
    v = obj.get(field)
    if field in ("src", "dst") and isinstance(v, dict):
        return _norm_side(v)
    if v is None and isinstance(sample, str):
        return ""
    if v is None and field == "return_traffic":
        return False
    return v


def clean_ops(state: dict, ops) -> dict:
    """Draft zone and rule changes {key: {"kind", "object", "after"}} reduced to valid, real changes, each with
    ``before`` read from ``state``. A new object gets a fresh ``new:`` id; a removal carries the whole object."""
    out = {}
    items = ops.values() if isinstance(ops, dict) else ops if isinstance(ops, list) else []
    for op in items:
        if not isinstance(op, dict) or op.get("kind") not in ZONE_KINDS or not isinstance(op.get("after", {}), dict):
            continue
        kind = stepkinds.KINDS[op["kind"]]
        after = {f: _norm(f, v) for f, v in (op.get("after") or {}).items() if f in kind.fields}
        obj_id = op.get("object")
        if kind.verb == "create":
            if not isinstance(obj_id, str) or not stepkinds.NEW_ID.match(obj_id):
                obj_id = "new:" + secrets.token_hex(8)
            if kind.name == "policy.create":
                after = dict(POLICY_DEFAULTS, **after)
                if not after.get("ports"):
                    after.pop("ports", None)
            if kind.name == "zone.create":
                after = {k: v for k, v in after.items() if k == "name"}
            step = {"kind": kind.name, "object": obj_id, "before": {}, "after": after}
        elif kind.verb == "delete":
            obj = _by_id(state, kind.area).get(obj_id)
            if not obj:
                continue
            before = {}
            for f, check in kind.fields.items():
                v = _cur(obj, f, "")
                if v in (None, ""):
                    continue
                try:
                    check(v)
                except stepkinds.FieldError:
                    continue
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
    area = stepkinds.KINDS[step["kind"]].area
    obj = _by_id(state, area).get(step.get("object")) or {}
    return obj.get("name") or (step.get("after") or {}).get("name") or (step.get("before") or {}).get("name") or "?"


def _zone_name(state, zid):
    return (_by_id(state, "zones").get(zid) or {}).get("name") or "?"


def preview(state: dict, ops: dict, *, lockout=None) -> dict:
    """Steps, plain-language changes, warnings (with the confirmation token of confirmed steps), blocks and
    confirmations for drafted zone and rule changes. ``lockout``: an extra family rule (step -> key or None)."""
    from . import model
    steps, changes, warnings, blocks, confirm = [], [], [], [], []
    # in the order of the draft (never sorted by id): new rules of one zone pair are created - and so ordered - the way
    # they were drafted
    for key in ops:
        st = ops[key]
        name = label(state, st)
        kind = st["kind"]
        why = model.lockout_reason(state, st) if stepkinds.KINDS[kind].verb != "create" else None
        why = why or step_lockout(state, st) or (lockout(st) if lockout else None)
        if why:
            blocks.append([why, {"name": name, "reason": ""}])
            continue
        steps.append(st)
        order = lambda kv: (FIELD_ORDER.index(kv[0]) if kv[0] in FIELD_ORDER else 99, kv[0])
        verb = stepkinds.KINDS[kind].verb
        if verb == "create":
            edit = [{"field": f, "from": "", "to": v} for f, v in sorted(st["after"].items(), key=order) if v not in ("", None)]
        elif verb == "delete":
            edit = [{"field": f, "from": v, "to": ""} for f, v in sorted(st["before"].items(), key=order) if v not in ("", None)]
        else:
            edit = [{"field": f, "from": st["before"].get(f), "to": v} for f, v in sorted(st["after"].items(), key=order)]
        changes.append({"device": st["object"], "name": name, "kind": kind, "edit": edit, "what": WHAT[kind]})
        token = stepkinds.confirm_token(st) if (stepkinds.needs_confirmation(st) or needs_confirmation(state, st)) else None
        if token:
            confirm.append(token)
        after = st["after"]
        if kind in ("policy.create", "policy.update") and token:
            old = _by_id(state, "policies").get(st["object"]) or {}
            merged = dict(old, **after)
            warnings.append(["zf.warn.pol_opens", {"name": name, "from": _zone_name(state, (merged.get("src") or {}).get("zone")),
                                                   "to": _zone_name(state, (merged.get("dst") or {}).get("zone")), "confirm": token}])
        elif kind == "zone.delete":
            warnings.append(["zf.warn.zone_delete", {"name": name}])
        elif kind == "policy.update" and after.get("enabled") is False:
            warnings.append(["zf.warn.pol_off", {"name": name}])
    why, vars_ = plan_lockout_vars(state, steps)
    if why:
        blocks.append([why, vars_])
    return {"steps": steps, "changes": changes, "warnings": warnings, "blocks": blocks, "confirm": confirm}


def review(state: dict, steps: list, *, requester: str = None, sources=(), targets=()) -> dict:
    """The reachability part of a preview over all drafted steps: which zone pairs open or close (warnings) and a
    block when a guarded path would be cut."""
    warnings, blocks = [], []
    relevant = any(str(st.get("kind", "")).startswith(("zone.", "policy.")) or
                   (st.get("kind") in ("network.update", "network.create") and (st.get("after") or {}).get("zone") is not None)
                   for st in steps)
    if not relevant and not any(st.get("kind") in ("move", "client.set") for st in steps):
        return {"warnings": warnings, "blocks": blocks, "pairs": []}
    after = after_state(state, steps)
    pairs = []
    if relevant:
        b, a = zone_matrix(state), zone_matrix(after)
        names = {z["id"]: z.get("name") or "?" for z in (after.get("zones") or []) + (state.get("zones") or [])}
        for k in sorted(set(b) | set(a), key=lambda k: (names.get(k[0], ""), names.get(k[1], ""))):
            vb, va = b.get(k, "block"), a.get(k, "block")
            if vb == va:
                continue
            if len(pairs) < MAX_REACH_PAIRS:
                pairs.append({"from": k[0], "to": k[1], "before": vb, "after": va})
            if va == "allow" and vb != "allow":
                warnings.append(["zf.warn.pair_opens", {"from": names.get(k[0], "?"), "to": names.get(k[1], "?")}])
            elif va == "block" and vb != "block":
                warnings.append(["zf.warn.pair_closes", {"from": names.get(k[0], "?"), "to": names.get(k[1], "?")}])
        warnings = warnings[:12]
    paths = guard_paths(state, requester=requester, sources=sources, targets=targets)
    hit = reach_lockout(state, after, paths) if paths else None
    if hit:
        blocks.append([hit[0], hit[1]])
    return {"warnings": warnings, "blocks": blocks, "pairs": pairs}
