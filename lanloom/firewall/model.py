"""Controller state, the reachability evaluator, preview and plan building, and the gate.

State format (what an adapter publishes; device ids are opaque hashes, never hardware addresses)::

    {"controller": {"label": str, "kind": "simulation" | "controller"},
     "zones":    [{"id", "name", "internet": bool, "discovery": bool}],
     "allow":    [[from_zone, to_zone], ...]          # "*" matches any zone; same zone is always allowed
     "networks": [{"id", "name", "zone", "cidr", "vlan": int, "management": bool}],
     "devices":  [{"id", "name", "kind", "network", "ip", "fixed": bool, "wired": bool,
                   "alone_on_port": bool | None, "private_address": bool, "protected": reason | None}],
     "dependencies": [{"device": id, "by": str}],
     "side_effects": {"wireless_move": "reprovision_all" | None}}

A plan step only names ids. The executor resolves them from its own fresh read of the controller.
"""
import ipaddress
import secrets
import time

from .canon import digest_hex, fingerprint

MAX_OBJECTS = 10
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
        elif nid == device.get("network"):
            out[nid] = "fw.why.same"
        elif net.get("management"):
            out[nid] = "fw.why.management"
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


def preview(state: dict, moves: dict, *, requester_ip: str = None, quiet: bool = False) -> dict:
    """Draft moves {device_id: network_id} -> steps, plain-language consequences, warnings and blocks."""
    zones, nets, devs = index(state)
    steps, changes, warnings, blocks = [], [], [], []
    confirm = []
    taken = {}
    requester = next((d for d in devs.values() if requester_ip and d.get("ip") == requester_ip), None)
    deps = {}
    for dep in state.get("dependencies") or []:
        deps.setdefault(dep.get("device"), []).append(dep.get("by"))
    wireless_moved = False
    for dev_id, net_id in sorted(moves.items()):
        dev, net = devs.get(dev_id), nets.get(net_id)
        if not dev or not net:
            blocks.append(["fw.block.unknown", {}])
            continue
        why = drop_targets(state, dev).get(net_id)
        if why == "fw.why.same":
            continue
        if why:
            blocks.append([{"fw.why.protected": "fw.block.protected", "fw.why.management": "fw.block.management",
                            "fw.why.shared_port": "fw.block.shared_port"}[why], {"name": dev["name"], "reason": dev.get("protected") or ""}])
            continue
        old_net = nets.get(dev["network"]) or {"name": "?", "zone": None}
        new_ip = pick_address(state, dev, net, taken.get(net_id, ()))
        taken.setdefault(net_id, []).append(new_ip)
        steps.append({"kind": "move", "device": dev_id,
                      "before": {"network": dev["network"], "ip": dev.get("ip") or ""},
                      "after": {"network": net_id, "ip": new_ip}})
        before, after = abilities(state, dev["network"]), abilities(state, net_id)
        b_s, a_s = sentences(state, before), sentences(state, after)
        changes.append({"device": dev_id, "name": dev["name"], "kind": dev.get("kind"), "from": old_net["name"], "to": net["name"],
                        "from_zone": (zones.get(old_net.get("zone")) or {}).get("name"), "to_zone": (zones.get(net.get("zone")) or {}).get("name"),
                        "old_ip": dev.get("ip"), "new_ip": new_ip,
                        "may": a_s, "may_not": [s for s in b_s if s not in a_s]})
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
        for by in deps.get(dev_id, []):
            warnings.append(["fw.warn.dependency", {"name": dev["name"], "by": by}])
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
    now = int(now or time.time())
    return {"v": 1, "id": "p_%d_%s" % (now, secrets.token_hex(4)), "target": target, "base": pv["base"],
            "created": now, "expires": now + PLAN_TTL_S, "nonce": secrets.token_hex(16),
            "requester": {"user": user, "device": pv.get("requester")}, "steps": pv["steps"],
            "warnings": sorted({w[0] for w in pv["warnings"]}), "confirmed": sorted(set(confirmed)),
            "costs": pv["costs"], "undo_of": undo_of}


def plan_hash(plan: dict) -> str:
    return digest_hex(plan)


def validate_plan(plan) -> None:
    """Shape check before anything else looks at a plan."""
    if not isinstance(plan, dict) or plan.get("v") != 1:
        raise Refused("fw.block.format")
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


def gate(state: dict, plan: dict, *, target: str, now: float, quiet: bool, busy: bool, healthy: tuple) -> None:
    """Hard checks right before writing, against a fresh controller read. Raises Refused."""
    validate_plan(plan)
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
                           "fw.why.shared_port": "fw.block.shared_port", "fw.why.same": "fw.block.format"}[why],
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


def apply_moves(state: dict, steps: list) -> dict:
    """The state after the steps (used by the simulation and to check a plan against itself)."""
    import copy
    new = copy.deepcopy(state)
    by_id = {d["id"]: d for d in new.get("devices") or []}
    for st in steps:
        d = by_id[st["device"]]
        d["network"], d["ip"] = st["after"]["network"], st["after"]["ip"]
    return new


def inverse(steps: list) -> list:
    return [{"kind": "move", "device": s["device"], "before": dict(s["after"]), "after": dict(s["before"])} for s in reversed(steps)]


def in_quiet_hours(spec: str, now: float) -> bool:
    """spec "21-7" = from 21:00 to 07:00 local time; empty = never."""
    try:
        a, b = (int(x) for x in (spec or "").split("-", 1))
    except ValueError:
        return False
    hour = time.localtime(now).tm_hour
    return (a <= hour or hour < b) if a > b else (a <= hour < b)
