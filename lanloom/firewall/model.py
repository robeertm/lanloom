"""Controller state, the reachability evaluator, preview and plan building, and the gate.

State format (what an adapter publishes; device ids are opaque hashes, never hardware addresses)::

    {"controller": {"label": str, "kind": "simulation" | "controller"},
     "zones":    [{"id", "name", "internet": bool, "discovery": bool}],
     "allow":    [[from_zone, to_zone], ...]          # "*" matches any zone; same zone is always allowed
     "networks": [{"id", "name", "zone", "cidr", "vlan": int, "management": bool}],
     "devices":  [{"id", "name", "kind", "network", "ip", "fixed": bool, "wired": bool,
                   "alone_on_port": bool | None, "private_address": bool, "protected": reason | None}],
     "dependencies": [{"device": id, "by": str, "via"?: integration|tracker|automation|lanloom}],
     "side_effects": {"wireless_move": "reprovision_all" | None}}

A plan step only names ids. The executor resolves them from its own fresh read of the controller.
"""
import ipaddress
import secrets
import time

from . import neutral, netconf, portconf, steps as stepkinds, zonefw
from .canon import digest_hex, fingerprint

from .steps import MAX_OBJECTS  # noqa: E402  (re-exported: model.MAX_OBJECTS)
PLAN_TTL_S = 300
CLIENT_KINDS = {"computer", "laptop", "desktop", "phone", "tablet", "workstation"}


class Refused(Exception):
    """A gate refusal. ``key`` is a translation key, ``vars`` its parameters."""

    def __init__(self, key: str, **vars):
        super().__init__(key)
        self.key = key
        self.vars = vars


def index(state: dict) -> tuple:
    zones = {z["id"]: z for z in state.get("zones") or []}
    nets = {n["id"]: n for n in state.get("networks") or []}
    devs = {d["id"]: d for d in state.get("devices") or []}
    return zones, nets, devs


def zone_allows(state: dict, src: str, dst: str) -> bool:
    if src == dst:
        return True
    for a, b in state.get("allow") or []:
        if a in (src, "*") and b in (dst, "*"):
            return True
    return False


def abilities(state: dict, network_id: str) -> dict:
    """What a device in this network may do: internet, zones it may reach, zones that may reach it, discovery."""
    zones, nets, _ = index(state)
    net = nets.get(network_id) or {}
    zid = net.get("zone")
    zone = zones.get(zid) or {}
    reach = sorted(z for z in zones if z != zid and zone_allows(state, zid, z))
    reached = sorted(z for z in zones if z != zid and zone_allows(state, z, zid))
    return {"internet": bool(zone.get("internet")), "reach": reach, "reached_by": reached, "zone": zid,
            "discovery": bool(zone.get("discovery"))}


def sentences(state: dict, ab: dict) -> list:
    """Abilities as sentence items for the preview: [key, vars]."""
    zones, _, _ = index(state)
    out = [["fw.can.internet", {}]] if ab["internet"] else []
    for z in ab["reach"]:
        out.append(["fw.can.reach", {"zone": zones[z]["name"]}])
    for z in ab["reached_by"]:
        out.append(["fw.can.reached_by", {"zone": zones[z]["name"]}])
    if ab["discovery"]:
        out.append(["fw.can.discovery", {}])
    return out


def drop_targets(state: dict, device: dict) -> dict:
    """network id -> None (allowed) or a reason key why the device cannot go there."""
    _, nets, _ = index(state)
    out = {}
    for nid, net in nets.items():
        if device.get("protected"):
            out[nid] = "fw.why.protected"
        elif not device.get("network"):
            out[nid] = "fw.why.no_network"
        elif nid == device.get("network"):
            out[nid] = "fw.why.same"
        elif net.get("management"):
            out[nid] = "fw.why.management"
        elif device.get("network_by_port"):
            out[nid] = "fw.why.wired_port"
        elif device.get("wired") and device.get("alone_on_port") is False:
            out[nid] = "fw.why.shared_port"
        else:
            out[nid] = None
    return out


def pick_address(state: dict, device: dict, target_net: dict, taken_extra=()) -> str:
    """Same last number in the target network when free, otherwise the next free address from .20 upwards."""
    net = ipaddress.ip_network(target_net["cidr"], strict=False)
    taken = {d.get("ip") for d in state.get("devices") or [] if d.get("network") == target_net["id"]} | set(taken_extra)
    hosts = list(net.hosts())
    gateway = str(hosts[0]) if hosts else None
    try:
        last = int(str(device.get("ip") or "").rsplit(".", 1)[1])
        same = str(net.network_address + last)
        if ipaddress.ip_address(same) in net and same not in taken and same != gateway and last not in (0, 255):
            return same
    except (IndexError, ValueError):
        pass
    for h in hosts[19:]:
        if str(h) not in taken:
            return str(h)
    raise Refused("fw.block.full", net=target_net["name"])


def _taken_addresses(state: dict, net: dict, skip: str = None) -> set:
    """Addresses in a network that belong to another device (its current address or its fixed address)."""
    out = set()
    for d in state.get("devices") or []:
        if d.get("id") == skip:
            continue
        for key in ("ip", "fixed_ip"):
            ip = d.get(key)
            if ip and d.get("network") == net["id"] or (ip and key == "fixed_ip" and _in(ip, net)):
                out.add(ip)
    return out


def _in(ip: str, net: dict) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(net.get("cidr") or "", strict=False)
    except ValueError:
        return False


def address_problem(state: dict, dev: dict, net: dict, ip: str):
    """Why ``ip`` cannot be the (fixed) address of ``dev`` in ``net``: a (key, vars) pair, or None when it can.
    Controllers do not all check this themselves (some store an address outside the network without a word)."""
    try:
        addr = ipaddress.ip_address(ip)
        cidr = ipaddress.ip_network(net.get("cidr") or "", strict=False)
    except ValueError:
        return "fw.block.ip_invalid", {"ip": str(ip)[:45], "name": dev.get("name", "")}
    if addr not in cidr or addr.version != cidr.version:
        return "fw.block.ip_outside", {"ip": ip, "net": net.get("name", ""), "cidr": str(cidr), "name": dev.get("name", "")}
    hosts = list(cidr.hosts()) if cidr.num_addresses <= 65536 else []
    gateway = net.get("gateway_ip") or (str(hosts[0]) if hosts else None)
    if addr in (cidr.network_address, cidr.broadcast_address) or ip == gateway:
        return "fw.block.ip_reserved", {"ip": ip, "name": dev.get("name", "")}
    if ip in _taken_addresses(state, net, skip=dev.get("id")):
        return "fw.block.ip_taken", {"ip": ip, "name": dev.get("name", "")}
    return None


def _cur(dev: dict, field: str):
    """A device's current value in the shape of a plan step (no value = empty text)."""
    v = dev.get(field)
    return "" if v is None and field in ("fixed_ip", "ip", "name") else v


EDIT_FIELDS = ("name", "fixed_ip")


def clean_edits(state: dict, edits) -> dict:
    """Draft edits {device_id: {"name"?, "fixed_ip"?}} reduced to valid, real changes (unknown devices, unknown fields,
    invalid values and values equal to the current one are dropped)."""
    _, _, devs = index(state)
    out = {}
    for dev_id, fields in (edits or {}).items():
        dev = devs.get(dev_id) if isinstance(dev_id, str) else None
        if not dev or not isinstance(fields, dict):
            continue
        keep = {}
        for f, v in fields.items():
            if f not in EDIT_FIELDS or not isinstance(v, str):
                continue
            v = v.strip()
            try:
                stepkinds.KINDS["client.set"].fields[f](v)
            except stepkinds.FieldError:
                continue
            if v != _cur(dev, f):
                keep[f] = v
        if keep:
            out[dev_id] = keep
    return out


def _edit_only(state, dev, edit, steps, changes, warnings, blocks, confirm, deps, requester, taken) -> None:
    """A device that stays in its network and only gets a new name and/or fixed address."""
    if dev.get("protected"):
        blocks.append(["fw.block.protected", {"name": dev["name"], "reason": dev.get("protected") or ""}])
        return
    _, nets, _ = index(state)
    net = nets.get(dev.get("network"))
    if "fixed_ip" in edit and edit["fixed_ip"]:
        if not net:
            blocks.append(["fw.block.no_network", {"name": dev.get("name", "")}])
            return
        prob = address_problem(state, dev, net, edit["fixed_ip"]) or (
            ("fw.block.ip_taken", {"ip": edit["fixed_ip"], "name": dev["name"]}) if edit["fixed_ip"] in taken.get(net["id"], ()) else None)
        if prob:
            blocks.append([prob[0], prob[1]])
            return
        taken.setdefault(net["id"], []).append(edit["fixed_ip"])
    after = dict(edit)
    steps.append({"kind": "client.set", "object": dev["id"], "before": {f: _cur(dev, f) for f in after}, "after": after})
    fields = [{"field": f, "from": _cur(dev, f), "to": v} for f, v in sorted(after.items())]
    changes.append({"device": dev["id"], "name": dev["name"], "kind": dev.get("kind"), "edit": fields})
    if "fixed_ip" in edit:
        if edit["fixed_ip"]:
            warnings.append(["fw.warn.fixed_ip_lease", {"name": dev["name"], "ip": edit["fixed_ip"]}])
        else:
            warnings.append(["fw.warn.fixed_ip_removed", {"name": dev["name"]}])
            for dep in deps.get(dev["id"], []):
                warnings.append(netconf.dep_warning(dev["name"], dep))
        if requester and requester["id"] == dev["id"]:
            warnings.append(["fw.warn.self", {"name": dev["name"]}])
            confirm.append("self")


def preview(state: dict, moves: dict, *, requester_ip: str = None, quiet: bool = False, edits: dict = None) -> dict:
    """Draft moves {device_id: network_id} and edits {device_id: {"name"?, "fixed_ip"?}} -> steps, plain-language
    consequences, warnings and blocks. A device that is only moved gives a ``move`` step (plans of moves only stay
    v1); a device with an edit gives one ``client.set`` step that carries every changed field, a move included."""
    zones, nets, devs = index(state)
    edits = edits or {}
    steps, changes, warnings, blocks = [], [], [], []
    confirm = []
    taken = {}
    requester = next((d for d in devs.values() if requester_ip and d.get("ip") == requester_ip), None)
    deps = {}
    for dep in state.get("dependencies") or []:
        if isinstance(dep, dict):
            deps.setdefault(dep.get("device"), []).append(dep)
    wireless_moved = False
    for dev_id in sorted(set(moves) | set(edits)):
        net_id = moves.get(dev_id)
        dev = devs.get(dev_id)
        edit = edits.get(dev_id) or {}
        if not dev or (net_id is not None and not nets.get(net_id)):
            blocks.append(["fw.block.unknown", {}])
            continue
        if net_id is None or net_id == dev.get("network"):
            if edit:
                _edit_only(state, dev, edit, steps, changes, warnings, blocks, confirm, deps, requester, taken)
            continue
        if not dev.get("network"):
            blocks.append(["fw.block.no_network", {"name": dev.get("name", "")}])
            continue
        net = nets[net_id]
        why = drop_targets(state, dev).get(net_id)
        if why:
            blocks.append([{"fw.why.protected": "fw.block.protected", "fw.why.management": "fw.block.management",
                            "fw.why.shared_port": "fw.block.shared_port", "fw.why.no_network": "fw.block.no_network", "fw.why.wired_port": "fw.block.wired_move"}[why], {"name": dev["name"], "reason": dev.get("protected") or ""}])
            continue
        old_net = nets.get(dev["network"]) or {"name": "?", "zone": None}
        if edit.get("fixed_ip"):
            new_ip = edit["fixed_ip"]
            prob = address_problem(state, dev, net, new_ip) or (
                ("fw.block.ip_taken", {"ip": new_ip, "name": dev["name"]}) if new_ip in taken.get(net_id, ()) else None)
            if prob:
                blocks.append([prob[0], prob[1]])
                continue
        else:
            new_ip = pick_address(state, dev, net, taken.get(net_id, ()))
        taken.setdefault(net_id, []).append(new_ip)
        if edit:
            after = {"network": net_id, "fixed_ip": new_ip}
            if edit.get("name"):
                after["name"] = edit["name"]
            steps.append({"kind": "client.set", "object": dev_id, "before": {f: _cur(dev, f) if f != "network" else dev["network"] for f in after},
                          "after": after})
        else:
            steps.append({"kind": "move", "device": dev_id,
                          "before": {"network": dev["network"], "ip": dev.get("ip") or ""},
                          "after": {"network": net_id, "ip": new_ip}})
        before, after = abilities(state, dev["network"]), abilities(state, net_id)
        b_s, a_s = sentences(state, before), sentences(state, after)
        changes.append({"device": dev_id, "name": dev["name"], "kind": dev.get("kind"), "from": old_net["name"], "to": net["name"],
                        "from_zone": (zones.get(old_net.get("zone")) or {}).get("name"), "to_zone": (zones.get(net.get("zone")) or {}).get("name"),
                        "old_ip": dev.get("ip"), "new_ip": new_ip,
                        "may": a_s, "may_not": [s for s in b_s if s not in a_s],
                        "edit": [{"field": "name", "from": dev["name"], "to": edit["name"]}] if edit.get("name") else []})
        warnings.append(["fw.warn.new_ip", {"name": dev["name"], "old": dev.get("ip") or "?", "new": new_ip}])
        if not dev.get("wired"):
            wireless_moved = True
        else:
            warnings.append(["fw.warn.wired_relink", {"name": dev["name"]}])
        if dev.get("private_address") and not dev.get("wired"):
            warnings.append(["fw.warn.private_address", {"name": dev["name"]}])
            confirm.append("private_address:" + dev_id)
        if before["internet"] and not after["internet"]:
            warnings.append(["fw.warn.no_internet", {"name": dev["name"], "net": net["name"]}])
        if before["discovery"] and not after["discovery"] and dev.get("kind") in CLIENT_KINDS:
            warnings.append(["fw.warn.discovery", {"name": dev["name"], "net": net["name"]}])
        for dep in deps.get(dev_id, []):
            warnings.append(netconf.dep_warning(dev["name"], dep))
        if requester and requester["id"] == dev_id:
            warnings.append(["fw.warn.self", {"name": dev["name"]}])
            confirm.append("self")
            if not after["internet"]:
                blocks.append(["fw.block.lockout", {"name": dev["name"]}])
    side = (state.get("side_effects") or {}).get("wireless_move")
    if wireless_moved and side == "reprovision_all":
        warnings.insert(0, ["fw.warn.reprovision", {}])
    if len(steps) > MAX_OBJECTS:
        blocks.append(["fw.block.too_many", {"n": len(steps), "max": MAX_OBJECTS}])
    if quiet and steps:
        warnings.append(["fw.warn.quiet", {}])
        confirm.append("quiet")
    return {"base": fingerprint(state), "steps": steps, "changes": changes, "warnings": warnings, "blocks": blocks,
            "confirm": confirm, "costs": {"objects": len(steps)}, "requester": requester["id"] if requester else None}


def build_plan(state: dict, pv: dict, *, target: str, user: str, confirmed: list, undo_of: str = None, now: int = None) -> dict:
    """The plan keeps the lowest version that expresses its steps: moves only -> v1 (as in earlier releases)."""
    now = int(now or time.time())
    return {"v": stepkinds.plan_version(pv["steps"]), "id": "p_%d_%s" % (now, secrets.token_hex(4)), "target": target, "base": pv["base"],
            "created": now, "expires": now + PLAN_TTL_S, "nonce": secrets.token_hex(16),
            "requester": {"user": user, "device": pv.get("requester")}, "steps": pv["steps"],
            "warnings": sorted({w[0] for w in pv["warnings"]}), "confirmed": sorted(set(confirmed)),
            "costs": pv["costs"], "undo_of": undo_of}


def plan_hash(plan: dict) -> str:
    return digest_hex(plan)


def validate_plan(plan) -> None:
    """Shape check before anything else looks at a plan. v1: moves only; v2: every kind in ``steps.KINDS``.
    A plan version above ``PLAN_VERSION_MAX`` is refused as such (never half-understood)."""
    if not isinstance(plan, dict) or isinstance(plan.get("v"), bool) or not isinstance(plan.get("v"), int):
        raise Refused("fw.block.format")
    if plan["v"] > stepkinds.PLAN_VERSION_MAX:
        raise Refused("fw.block.version", v=plan["v"], max=stepkinds.PLAN_VERSION_MAX)
    if plan["v"] < 1:
        raise Refused("fw.block.format")
    if plan["v"] == 2:
        return _validate_v2(plan)
    for key, typ in (("id", str), ("target", str), ("base", str), ("created", int), ("expires", int), ("nonce", str),
                     ("steps", list), ("confirmed", list), ("requester", dict)):
        if not isinstance(plan.get(key), typ) or isinstance(plan.get(key), bool):
            raise Refused("fw.block.format")
    if not plan["steps"]:
        raise Refused("fw.block.empty")
    for st in plan["steps"]:
        if not isinstance(st, dict) or st.get("kind") != "move" or set(st) != {"kind", "device", "before", "after"}:
            raise Refused("fw.block.format")
        for side in ("before", "after"):
            if not isinstance(st[side], dict) or set(st[side]) != {"network", "ip"}:
                raise Refused("fw.block.format")
            if not isinstance(st[side]["network"], str) or not isinstance(st[side]["ip"], str):
                raise Refused("fw.block.format")
            if side == "after" or st[side]["ip"]:
                try:
                    ipaddress.ip_address(st[side]["ip"])
                except ValueError:
                    raise Refused("fw.block.format")


def _validate_v2(plan) -> None:
    for key, typ in (("id", str), ("target", str), ("base", str), ("created", int), ("expires", int), ("nonce", str),
                     ("steps", list), ("confirmed", list), ("requester", dict)):
        if not isinstance(plan.get(key), typ) or isinstance(plan.get(key), bool):
            raise Refused("fw.block.format")
    if not plan["steps"]:
        raise Refused("fw.block.empty")
    if stepkinds.plan_version(plan["steps"]) != 2:
        raise Refused("fw.block.format")          # a plan of moves only must stay v1
    for st in plan["steps"]:
        if isinstance(st, dict) and st.get("kind") not in stepkinds.KINDS:
            raise Refused("fw.block.kind", kind=str(st.get("kind"))[:40])
        try:
            stepkinds.check_step(st)
        except stepkinds.FieldError:
            raise Refused("fw.block.format")


def gate(state: dict, plan: dict, *, target: str, now: float, quiet: bool, busy: bool, healthy: tuple, caps: dict = None) -> None:
    """Hard checks right before writing, against a fresh controller read. Raises Refused.
    ``caps``: the adapter's capability list (None = the v1 contract: moves only)."""
    validate_plan(plan)
    if plan["v"] == 2:
        return _gate_v2(state, plan, target=target, now=now, quiet=quiet, busy=busy, healthy=healthy, caps=caps)
    if plan["target"] != target:
        raise Refused("fw.block.target")
    if not (plan["created"] - 60 <= now <= plan["expires"]) or plan["expires"] - plan["created"] > 600:
        raise Refused("fw.block.expired")
    if busy:
        raise Refused("fw.block.busy")
    ok, why = healthy
    if not ok:
        raise Refused("fw.block.health", why=why or "")
    if len(plan["steps"]) > MAX_OBJECTS:
        raise Refused("fw.block.too_many", n=len(plan["steps"]), max=MAX_OBJECTS)
    check_capabilities(plan, caps)
    if fingerprint(state) != plan["base"]:
        raise Refused("fw.block.conflict")
    zones, nets, devs = index(state)
    seen = set()
    for st in plan["steps"]:
        dev, net = devs.get(st["device"]), nets.get(st["after"]["network"])
        if not dev or not net or st["device"] in seen:
            raise Refused("fw.block.unknown")
        seen.add(st["device"])
        if dev["network"] != st["before"]["network"] or (dev.get("ip") or "") != st["before"]["ip"]:
            raise Refused("fw.block.conflict")
        why = drop_targets(state, dev).get(net["id"])
        if why:
            raise Refused({"fw.why.protected": "fw.block.protected", "fw.why.management": "fw.block.management",
                           "fw.why.shared_port": "fw.block.shared_port", "fw.why.same": "fw.block.format",
                           "fw.why.no_network": "fw.block.no_network", "fw.why.wired_port": "fw.block.wired_move"}[why],
                          name=dev["name"], reason=dev.get("protected") or "")
        if ipaddress.ip_address(st["after"]["ip"]) not in ipaddress.ip_network(net["cidr"], strict=False):
            raise Refused("fw.block.format")
        clash = [d for d in devs.values() if d["network"] == net["id"] and d.get("ip") == st["after"]["ip"]]
        if clash:
            raise Refused("fw.block.conflict")
        req = plan["requester"].get("device")
        if req == st["device"]:
            if "self" not in plan["confirmed"]:
                raise Refused("fw.block.confirm")
            if not abilities(state, net["id"])["internet"]:
                raise Refused("fw.block.lockout", name=dev["name"])
        if dev.get("private_address") and not dev.get("wired") and ("private_address:" + dev["id"]) not in plan["confirmed"]:
            raise Refused("fw.block.confirm")
    if quiet and "quiet" not in plan["confirmed"]:
        raise Refused("fw.block.quiet")


def check_capabilities(plan: dict, caps: dict = None) -> None:
    """Every step must be within the adapter's capability list; stage C is refused in this release."""
    from ..integrations import capabilities as capmod
    caps = caps or capmod.V1_DEFAULT
    limit = min(MAX_OBJECTS, (caps.get("limits") or {}).get("objects_per_plan") or MAX_OBJECTS)
    if len(plan["steps"]) > limit:
        raise Refused("fw.block.too_many", n=len(plan["steps"]), max=limit)
    for st in plan["steps"]:
        stage = capmod.stage_for(caps, st)
        if stage is None:
            raise Refused("fw.block.capability", kind=st["kind"])
        if stage == "C" and not capmod.STAGE_C_ENABLED:
            raise Refused("fw.block.stage", kind=st["kind"])


def _gate_common(state, plan, *, target, now, busy, healthy) -> None:
    if plan["target"] != target:
        raise Refused("fw.block.target")
    if not (plan["created"] - 60 <= now <= plan["expires"]) or plan["expires"] - plan["created"] > 600:
        raise Refused("fw.block.expired")
    if busy:
        raise Refused("fw.block.busy")
    ok, why = healthy
    if not ok:
        raise Refused("fw.block.health", why=why or "")
    if fingerprint(state) != plan["base"]:
        raise Refused("fw.block.conflict")


def lockout_reason(state: dict, step: dict):
    """Neutral lockout rules for every family (adapters may add their own). Returns a refusal key or None."""
    kind = stepkinds.KINDS[step["kind"]]
    obj = neutral.find(state, step) or {}
    if obj.get("protected"):
        return "fw.block.protected"
    if obj.get("editable") is False or obj.get("builtin"):
        return "zf.block.zone_builtin" if kind.area == "zones" else "fw.block.builtin"
    if kind.verb == "delete" and not obj.get("own") and kind.area != "dhcp_reservations":
        return "fw.block.not_own"                 # (a fixed address is released, not deleted: any may go)
    if kind.area == "networks" and obj.get("management"):
        return "fw.block.management"
    if kind.area == "ports" and obj.get("is_uplink"):
        return "fw.block.uplink"
    if kind.area == "policies" and kind.verb != "delete":
        after = dict(obj, **step["after"])
        # never a blocking rule from or to the management network or zone, or towards the gateway itself
        prot = zonefw.protected_zones(state) | {"*"}
        mnets = zonefw.management_networks(state)
        sides = [after.get("src") or {}, after.get("dst") or {}]
        if after.get("action") in ("block", "reject") and any(s.get("zone") in prot or s.get("net") in mnets for s in sides):
            return "zf.block.mgmt_rule"
    if kind.area == "devices":
        nets = {n["id"]: n for n in state.get("networks") or []}
        target = step["after"].get("network")
        if target and (nets.get(target) or {}).get("management"):
            return "fw.block.management"
    return None


def _now_value(obj: dict, field: str, planned):
    """The fresh value to compare with a step's ``before``: a missing text value counts as empty text."""
    v = obj.get(field)
    if field == "poe" and isinstance(v, dict):         # a port's power: the step carries on/off, the state the details
        return bool(v.get("on"))
    return "" if v is None and isinstance(planned, str) else v


def _gate_device(state: dict, dev: dict, st: dict) -> None:
    """Moves and client settings: the target network must take the device, the address must fit and be free."""
    nets = neutral.objects(state, "networks")
    after = st["after"]
    net_id = after.get("network") or dev.get("network")
    net = nets.get(net_id)
    if "network" in after and after["network"] != dev.get("network"):
        why = drop_targets(state, dev).get(after["network"]) if net else "fw.why.same"
        if why:
            raise Refused({"fw.why.protected": "fw.block.protected", "fw.why.management": "fw.block.management",
                           "fw.why.shared_port": "fw.block.shared_port", "fw.why.same": "fw.block.format",
                           "fw.why.no_network": "fw.block.no_network", "fw.why.wired_port": "fw.block.wired_move"}[why],
                          name=dev.get("name", ""), reason=dev.get("protected") or "")
    elif st["kind"] == "move":
        raise Refused("fw.block.format")
    if dev.get("protected"):
        raise Refused("fw.block.protected", name=dev.get("name", ""), reason=dev.get("protected") or "")
    ip = after.get("ip") if st["kind"] == "move" else after.get("fixed_ip")
    if ip:
        if not net:
            raise Refused("fw.block.unknown")
        prob = address_problem(state, dev, net, ip)
        if prob:
            raise Refused(prob[0], **prob[1])


def _gate_v2(state, plan, *, target, now, quiet, busy, healthy, caps) -> None:
    _gate_common(state, plan, target=target, now=now, busy=busy, healthy=healthy)
    check_capabilities(plan, caps)
    seen = set()
    req = plan["requester"].get("device")
    wanted = [(st["after"].get("network"), st["after"].get("ip") or st["after"].get("fixed_ip")) for st in plan["steps"]
              if stepkinds.KINDS[st["kind"]].area == "devices" and (st["after"].get("ip") or st["after"].get("fixed_ip"))]
    if len(set(ip for _, ip in wanted)) != len(wanted):
        raise Refused("fw.block.conflict")
    for st in plan["steps"]:
        kind = stepkinds.KINDS[st["kind"]]
        oid = stepkinds.step_object(st)
        if oid in seen:
            raise Refused("fw.block.unknown")
        seen.add(oid)
        obj = neutral.find(state, st)
        if kind.verb == "create":
            if obj is not None:
                raise Refused("fw.block.conflict")
        else:
            if obj is None:
                raise Refused("fw.block.unknown")
            if any(_now_value(obj, f, v) != v for f, v in st["before"].items()):
                raise Refused("fw.block.conflict")
        if kind.area == "devices" and kind.name in ("move", "client.set"):
            _gate_device(state, obj, st)
        why = lockout_reason(state, st) or zonefw.step_lockout(state, st) or \
            portconf.step_lockout(state, st, {"devices": [req] if req else []})
        if why:
            if kind.area == "ports":
                raise Refused(why, **portconf.block_vars(state, st))
            raise Refused(why, name=(obj or {}).get("name", "") or st["after"].get("name", ""), reason=(obj or {}).get("protected") or "")
        if req and oid == req and "self" not in plan["confirmed"]:
            raise Refused("fw.block.confirm")
        if (stepkinds.needs_confirmation(st) or zonefw.needs_confirmation(state, st) or portconf.needs_confirmation(state, st)) and \
                stepkinds.confirm_token(st) not in plan["confirmed"]:
            raise Refused("fw.block.confirm")
    # rules over the whole plan: rule order, and the approver's device keeps its way to the gateway
    why, vars_ = zonefw.plan_lockout_vars(state, plan["steps"], requester=req)
    if why:
        raise Refused(why, **vars_)
    why = portconf.plan_lockout(state, plan["steps"])
    if why:
        raise Refused(why)
    if quiet and "quiet" not in plan["confirmed"]:
        raise Refused("fw.block.quiet")


def apply_moves(state: dict, steps: list) -> dict:
    """The state after the steps (used by the simulation and to check a plan against itself)."""
    import copy
    new = copy.deepcopy(state)
    by_id = {d["id"]: d for d in new.get("devices") or []}
    for st in steps:
        d = by_id[st["device"]]
        d["network"], d["ip"] = st["after"]["network"], st["after"]["ip"]
        if d.get("fixed_ip"):
            d["fixed_ip"] = st["after"]["ip"]
    return new


def inverse(steps: list, created: dict = None) -> list:
    """The steps that undo ``steps``, newest first. ``created`` maps ``new:`` ids to the ids the adapter assigned."""
    created = created or {}
    out = [stepkinds.inverse_step(s, created.get(stepkinds.step_object(s))) for s in reversed(steps)]
    return [s for s in out if s is not None]          # an action (power cycling a port) has no way back


def in_quiet_hours(spec: str, now: float) -> bool:
    """spec "21-7" = from 21:00 to 07:00 local time; empty = never."""
    try:
        a, b = (int(x) for x in (spec or "").split("-", 1))
    except ValueError:
        return False
    hour = time.localtime(now).tm_hour
    return (a <= hour or hour < b) if a > b else (a <= hour < b)
