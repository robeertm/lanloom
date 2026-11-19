"""Switch ports on the neutral model (stage B): a port's name, its port profile, the network it carries untagged, which
networks it carries tagged, power over Ethernet on or off, and power cycling a port. Pure functions, no I/O.

Used twice, with the same rules: by the web app when it builds a preview and by the executor's write half right before
it writes (with its own fresh read). A refusal on either side stops the plan.

What may be written here, and what never:

* **ports of end devices** - name, profile, untagged network, tagged networks, power on/off, power cycle. A profile
  decides the networks of the port (the port's own network settings are kept, but not used while a profile is set).
* **never: infrastructure ports** - the uplink of a switch, the internet port of a gateway, a port behind which the
  controller reports network equipment (a switch or an access point on it, its uplink pointing here) or another device
  answers with a link-layer neighbour announcement (LLDP), ports in mirror or aggregation mode. Only their name may
  change.
* **never: the own path** - a port that carries a device Lanloom protects (gateway, server, hub, Lanloom's own host),
  the approver's device, the executor or the controller keeps its networks and its power.
* **networks** - only local networks (no internet uplink, no VPN). The management network as the untagged network of a
  port, or newly tagged on it, needs its own confirmation: whatever is plugged in then reaches the admin pages of the
  network equipment.
* **power** - only on ports that can deliver power, only "on" (the family's automatic mode) or "off"; passive or
  pass-through modes are left alone. Every change of power and every power cycle needs its own confirmation with the
  devices it switches off named.

The answers of ``step_lockout`` are refusal keys (``pc.block.*`` or the shared ``fw.block.*``); ``preview`` adds the
plain warnings (``pc.warn.*``) and the confirmation tokens.
"""
import copy

from . import steps as stepkinds

PORT_KINDS = ("port.set", "port.cycle")
NET_FIELDS = ("profile", "native_network", "native_vlan", "tagged_mode", "tagged")
FIELD_ORDER = ("name", "profile", "native_network", "native_vlan", "tagged_mode", "tagged", "poe", "cycle")
LOCAL_PURPOSES = ("lan", "iot", "guest", "", None)       # networks a port may carry; never internet uplinks or VPNs
TAGGED_MODES = ("all", "none", "custom")
WHAT = {"port.set": "pc.change.port", "port.cycle": "pc.change.cycle"}


# ---------------------------------------------------------------------------- small helpers
def _by_id(state: dict, area: str) -> dict:
    return {o["id"]: o for o in state.get(area) or [] if isinstance(o, dict) and "id" in o}


def poe_on(port: dict):
    """The configured power of a port (True/False), or None when the port cannot deliver power."""
    p = port.get("poe")
    if isinstance(p, dict):
        return bool(p.get("on")) if p.get("capable", True) else None
    return p if isinstance(p, bool) else None


def poe_capable(port: dict) -> bool:
    p = port.get("poe")
    return (isinstance(p, dict) and bool(p.get("capable", True))) or isinstance(p, bool)


def devices_on(state: dict, port: dict) -> list:
    """End devices the controller sees on this port (wired, parent = the port's switch, same port number)."""
    out = []
    for d in state.get("devices") or []:
        if d.get("wired") and d.get("parent") == port.get("device") and d.get("port") is not None \
                and str(d.get("port")) == str(port.get("idx")) and d.get("online", True) is not False:
            out.append(d)
    return out


def equipment_on(state: dict, port: dict) -> list:
    """Network equipment hanging on this port: its uplink names this switch and port, or the port's neighbour is one."""
    out = []
    for e in state.get("equipment") or []:
        up = e.get("uplink") or {}
        if up.get("device") == port.get("device") and up.get("port") is not None and str(up.get("port")) == str(port.get("idx")):
            out.append(e)
        elif port.get("lldp_neighbor") and e.get("id") == port.get("lldp_neighbor"):
            out.append(e)
    return out


def switch_of(state: dict, port: dict) -> dict:
    return _by_id(state, "equipment").get(port.get("device")) or {}


def label(state: dict, step_or_port: dict) -> str:
    """"Hall switch · Port 7" for a step or a port."""
    port = step_or_port if "idx" in step_or_port else _by_id(state, "ports").get(step_or_port.get("object")) or {}
    sw = switch_of(state, port).get("name") or "?"
    return "%s · %s" % (sw, port.get("name") or port.get("idx") or "?")


def profile(state: dict, pid):
    return _by_id(state, "port_profiles").get(pid) if pid else None


def effective(state: dict, port: dict, after: dict = None) -> dict:
    """The networks and power a port carries with ``after`` applied: a profile decides the networks (and the power,
    when the profile names it); without one the port's own settings count."""
    merged = dict(port, **(after or {}))
    if "poe" in (after or {}):
        merged["poe"] = after["poe"]
    out = {"profile": merged.get("profile") or "", "native_network": merged.get("native_network"),
           "native_vlan": merged.get("native_vlan"), "tagged_mode": merged.get("tagged_mode"),
           "tagged": list(merged.get("tagged") or []), "poe": poe_on(merged) if not isinstance(merged.get("poe"), bool) else merged["poe"]}
    prof = profile(state, out["profile"])
    if prof:
        for f in ("native_network", "native_vlan", "tagged_mode", "tagged"):
            if prof.get(f) is not None:
                out[f] = copy.deepcopy(prof[f])
        if isinstance(prof.get("poe"), bool) and out["poe"] is not None:
            out["poe"] = prof["poe"]
    if out["tagged_mode"] != "custom":
        out["tagged"] = []
    nets = _by_id(state, "networks")
    if out["native_network"] and out["native_vlan"] is None:
        out["native_vlan"] = (nets.get(out["native_network"]) or {}).get("vlan")
    return out


def infra_reason(state: dict, port: dict):
    """Why a port's networks and power are never changed, or None (its name may always change)."""
    if port.get("is_uplink"):
        return "fw.block.uplink"
    if port.get("wan"):
        return "pc.block.wan"
    if port.get("mode") not in (None, "switch"):
        return "pc.block.port_mode"
    if port.get("infra") or equipment_on(state, port):
        return "pc.block.infra"
    if port.get("lldp"):
        return "pc.block.lldp"
    return None


def management_networks(state: dict) -> set:
    return {n["id"] for n in state.get("networks") or [] if n.get("management")}


def _net_problem(state: dict, net_id):
    """A network a port may carry: it exists and is local (no internet uplink, no VPN)."""
    if net_id in (None, ""):
        return None
    net = _by_id(state, "networks").get(net_id)
    if net is None:
        return "fw.block.unknown"
    if net.get("purpose") not in LOCAL_PURPOSES:
        return "fw.block.network_kind"
    return None


# ---------------------------------------------------------------------------- the rules
def changes_networks(state: dict, port: dict, after: dict) -> bool:
    before, now = effective(state, port), effective(state, port, after)
    return any(before[f] != now[f] for f in ("native_network", "native_vlan", "tagged_mode", "tagged"))


def step_lockout(state: dict, step: dict, guard: dict = None):
    """A refusal key for one port step, or None. ``guard``: {"addresses": addresses whose way must stay open (Lanloom,
    the executor, the controller, the gateway, the approver), "devices": device ids that must keep their port (the
    approver's)}."""
    kind = step.get("kind")
    if kind not in PORT_KINDS:
        return None
    port = _by_id(state, "ports").get(step.get("object"))
    if port is None:
        return "fw.block.unknown"
    after = step.get("after") or {}
    if kind == "port.set" and set(after) <= {"name"}:
        return None                                   # a name never cuts anything off
    why = infra_reason(state, port)
    if why:
        return why
    if kind == "port.set":
        if "poe" in after:
            if not poe_capable(port):
                return "pc.block.no_poe"
            mode = (port.get("poe") or {}).get("mode") if isinstance(port.get("poe"), dict) else None
            if mode not in (None, "auto", "off"):
                return "pc.block.poe_mode"
            prof = profile(state, after.get("profile", port.get("profile")))
            if prof and isinstance(prof.get("poe"), bool):
                return "pc.block.profile_poe"
        if "profile" in after and after["profile"] and profile(state, after["profile"]) is None:
            return "fw.block.unknown"
        if after.get("profile") and set(after) & {"native_network", "native_vlan", "tagged_mode", "tagged"}:
            return "pc.block.profile_and_networks"
        eff = effective(state, port, after)
        for nid in [eff["native_network"]] + list(eff["tagged"]):
            why = _net_problem(state, nid)
            if why:
                return why
        if eff["native_network"] and eff["native_network"] in eff["tagged"]:
            return "pc.block.tagged_native"
    cuts = kind == "port.cycle" or ("poe" in after and after["poe"] is False) or \
        (kind == "port.set" and changes_networks(state, port, after))
    if not cuts:
        return None
    guard = guard or {}
    addrs = {a for a in guard.get("addresses") or () if a}
    for d in devices_on(state, port):
        if d.get("protected"):
            return "pc.block.protected"
        if d.get("id") in set(guard.get("devices") or ()):
            return "pc.block.self_path"
        if (d.get("ip") and d["ip"] in addrs) or (d.get("fixed_ip") and d["fixed_ip"] in addrs):
            return "pc.block.self_path"
    return None


def block_vars(state: dict, step: dict) -> dict:
    """The words a refusal names: the port and, where one is in the way, the device."""
    port = _by_id(state, "ports").get(step.get("object")) or {}
    devs = devices_on(state, port) if port else []
    eq = equipment_on(state, port) if port else []
    who = next((d for d in devs if d.get("protected")), None) or (eq[0] if eq else None) or (devs[0] if devs else None)
    return {"name": label(state, step) if port else "?", "device": (who or {}).get("name") or "", "reason": ""}


def needs_confirmation(state: dict, step: dict) -> bool:
    """Beyond the kind's own rule (power, power cycle): the management network as the untagged network of a port, or
    newly carried tagged on it."""
    if step.get("kind") != "port.set":
        return False
    port = _by_id(state, "ports").get(step.get("object"))
    if port is None:
        return False
    return bool(_to_management(state, port, step.get("after") or {}))


def _to_management(state: dict, port: dict, after: dict) -> list:
    mnets = management_networks(state)
    if not mnets:
        return []
    before, now = effective(state, port), effective(state, port, after)
    out = []
    if now["native_network"] in mnets and before["native_network"] != now["native_network"]:
        out.append(now["native_network"])
    out.extend(n for n in now["tagged"] if n in mnets and n not in before["tagged"] and before["tagged_mode"] != "all")
    if now["tagged_mode"] == "all" and before["tagged_mode"] != "all" and mnets - {now["native_network"]}:
        out.extend(sorted(mnets - {now["native_network"]}))
    return out


def plan_lockout(state: dict, steps: list):
    """Rules over the whole plan: one change per port (power cycle and settings of the same port do not mix)."""
    seen = set()
    for st in steps:
        if st.get("kind") in PORT_KINDS:
            if st.get("object") in seen:
                return "pc.block.one_per_port"
            seen.add(st.get("object"))
    return None


# ---------------------------------------------------------------------------- the state after a plan
def apply_to(state: dict, port: dict, after: dict) -> None:
    """Write ``after`` into a port of ``state`` (the simulation and the state after a plan)."""
    for f, v in after.items():
        if f == "poe":
            if isinstance(port.get("poe"), dict):
                port["poe"] = dict(port["poe"], on=bool(v), mode="auto" if v else "off")
                if not v:
                    port["poe"]["watts"] = 0
            else:
                port["poe"] = bool(v)
        elif f == "tagged":
            port["tagged"] = sorted(v)
        else:
            port[f] = copy.deepcopy(v)
    if after.get("tagged_mode") in ("all", "none"):
        port["tagged"] = []
    if "native_network" in after:
        port["native_vlan"] = (_by_id(state, "networks").get(after["native_network"]) or {}).get("vlan")
    if "profile" in after:
        prof = profile(state, after["profile"])
        port["profile_name"] = (prof or {}).get("name") if prof else None


def after_state(state: dict, steps: list) -> dict:
    new = copy.deepcopy(state)
    ports = _by_id(new, "ports")
    for st in steps:
        if st.get("kind") == "port.set" and st.get("object") in ports:
            apply_to(new, ports[st["object"]], st.get("after") or {})
    return new


def matches(state: dict, port: dict, after: dict) -> bool:
    """Does a port (freshly read) carry the values of a step's ``after``?"""
    for f, v in after.items():
        now = poe_on(port) if f == "poe" else port.get(f)
        if f == "tagged":
            now, v = sorted(now or []), sorted(v or [])
        if f == "name" and v in ("", None, str(port.get("idx"))):
            continue                                  # back to the family's default name: whatever it calls the port
        if now != v and not (f == "profile" and not now and not v):
            return False
    return True


# ---------------------------------------------------------------------------- drafts and preview (web app)
def op_key(kind: str, obj: str) -> str:
    return "%s|%s" % (kind, obj)


def _cur(port: dict, field: str):
    if field == "poe":
        return poe_on(port)
    v = port.get(field)
    if field == "profile":
        return v or "" if v is not None else ""
    if field == "tagged" and isinstance(v, list):
        return sorted(x for x in v if isinstance(x, str))
    if field == "name":
        return v if isinstance(v, str) else str(port.get("idx") or "")
    return v


def _norm(field: str, v):
    if isinstance(v, str):
        v = v.strip()
    if field == "tagged" and isinstance(v, (list, tuple)):
        v = sorted({str(x).strip() for x in v if isinstance(x, str) and x.strip()})
    if field == "native_vlan" and isinstance(v, str) and v.isdigit():
        v = int(v)
    if field == "poe" and not isinstance(v, bool):
        v = bool(v)
    return v


def _valid(check, value) -> bool:
    try:
        check(value)
        return True
    except stepkinds.FieldError:
        return False


def clean_ops(state: dict, ops) -> dict:
    """Draft port changes {key: {"kind", "object", "after"}} reduced to valid, real changes, each with ``before`` read
    from ``state``. A profile decides the networks: with one chosen, network fields of the same draft are dropped."""
    out = {}
    ports = _by_id(state, "ports")
    items = ops.values() if isinstance(ops, dict) else ops if isinstance(ops, list) else []
    for op in items:
        if not isinstance(op, dict) or op.get("kind") not in PORT_KINDS or not isinstance(op.get("after", {}), dict):
            continue
        port = ports.get(op.get("object"))
        if port is None:
            continue
        if op["kind"] == "port.cycle":
            step = {"kind": "port.cycle", "object": port["id"], "before": {}, "after": {"cycle": True}}
        else:
            kind = stepkinds.KINDS["port.set"]
            after = {f: _norm(f, v) for f, v in (op.get("after") or {}).items() if f in kind.fields}
            if after.get("profile"):
                for f in ("native_network", "native_vlan", "tagged_mode", "tagged"):
                    after.pop(f, None)
            if "tagged" in after and "tagged_mode" not in after:
                after["tagged_mode"] = "custom"
            if after.get("tagged_mode") in ("all", "none"):
                after["tagged"] = []
            native = after.get("native_network", port.get("native_network"))
            if isinstance(after.get("tagged"), list) and native:
                after["tagged"] = [x for x in after["tagged"] if x != native]
            keep = {}
            for f, v in after.items():
                cur = _cur(port, f)
                if cur is None and f != "native_vlan":
                    continue                          # the port does not report this setting: nothing to compare with
                if v != cur and _valid(kind.fields[f], v) and _valid(kind.fields[f], cur):
                    keep[f] = v
            if "tagged" in keep and "tagged_mode" not in keep and _cur(port, "tagged_mode") != "custom":
                keep["tagged_mode"] = "custom"
            if "tagged_mode" in keep and keep["tagged_mode"] != "custom" and "tagged" in keep and not keep["tagged"]:
                if _cur(port, "tagged") == []:
                    keep.pop("tagged")
            if not keep:
                continue
            step = {"kind": "port.set", "object": port["id"], "before": {f: _cur(port, f) for f in keep}, "after": keep}
        try:
            stepkinds.check_step(step)
        except stepkinds.FieldError:
            continue
        out[op_key(step["kind"], port["id"])] = step
    return out


def preview(state: dict, ops: dict, *, deps: dict = None, guard: dict = None, lockout=None) -> dict:
    """Steps, plain-language changes, warnings (with the confirmation token of confirmed steps), blocks and
    confirmations for drafted port changes. ``deps``: {device id: [{"by", "via"}]} - what uses a device by its address;
    ``guard``: see ``step_lockout``; ``lockout``: an extra family rule (step -> key or None)."""
    from . import model, netconf
    steps, changes, warnings, blocks, confirm = [], [], [], [], []
    deps = deps or {}
    ports = _by_id(state, "ports")
    nets = _by_id(state, "networks")
    order = lambda kv: (FIELD_ORDER.index(kv[0]) if kv[0] in FIELD_ORDER else 99, kv[0])
    for key in sorted(ops):
        st = ops[key]
        port = ports.get(st["object"]) or {}
        name = label(state, st)
        why = model.lockout_reason(state, st) or step_lockout(state, st, guard) or (lockout(st) if lockout else None)
        if why:
            blocks.append([why, block_vars(state, st)])
            continue
        steps.append(st)
        kind, after = st["kind"], st["after"]
        if kind == "port.cycle":
            edit = []
        else:
            edit = [{"field": f, "from": st["before"].get(f), "to": v} for f, v in sorted(after.items(), key=order)]
        changes.append({"device": st["object"], "name": name, "kind": kind, "edit": edit, "what": WHAT[kind]})
        token = stepkinds.confirm_token(st) if (stepkinds.needs_confirmation(st) or needs_confirmation(state, st)) else None
        if token:
            confirm.append(token)
        devs = devices_on(state, port)
        names = ", ".join(d.get("name") or "?" for d in devs[:3]) + (" +%d" % (len(devs) - 3) if len(devs) > 3 else "")
        if kind == "port.cycle":
            warnings.append(["pc.warn.cycle", {"name": name, "devices": names or "-", "confirm": token}])
            continue
        if "name" in after and len(after) == 1:
            warnings.append(["pc.warn.rename", {"name": name, "new": after["name"] or str(port.get("idx") or "")}])
            continue
        if "profile" in after:
            prof = profile(state, after["profile"])
            warnings.append(["pc.warn.profile" if prof else "pc.warn.profile_off",
                             {"name": name, "profile": (prof or {}).get("name") or ""}])
        if changes_networks(state, port, after):
            before_eff, eff = effective(state, port), effective(state, port, after)
            if before_eff["native_network"] != eff["native_network"] or before_eff["native_vlan"] != eff["native_vlan"]:
                net = nets.get(eff["native_network"]) or {}
                vlan = eff["native_vlan"]
                if devs:
                    warnings.append(["pc.warn.native", {"name": name, "net": net.get("name") or (("VLAN %s" % vlan) if vlan else "?"),
                                                        "devices": names, "n": len(devs)}])
                else:
                    warnings.append(["pc.warn.native_empty", {"name": name, "net": net.get("name") or (("VLAN %s" % vlan) if vlan else "?")}])
                for d in devs:
                    if d.get("fixed_ip"):
                        warnings.append(["pc.warn.fixed_ip", {"name": d.get("name") or "?", "ip": d["fixed_ip"]}])
                    for dep in deps.get(d.get("id"), []):
                        warnings.append(netconf.dep_warning(d.get("name") or "?", dep))
            if before_eff["tagged_mode"] != eff["tagged_mode"] or before_eff["tagged"] != eff["tagged"]:
                warnings.append(["pc.warn.tagged", {"name": name}])
            if len(devs) > 1:
                warnings.append(["pc.warn.shared", {"name": name, "n": len(devs)}])
            mg = _to_management(state, port, after)
            if mg:
                warnings.append(["pc.warn.to_management", {"name": name, "net": (nets.get(mg[0]) or {}).get("name") or "?",
                                                           "confirm": token}])
        if "poe" in after:
            if after["poe"]:
                warnings.append(["pc.warn.poe_on", {"name": name, "confirm": token}])
            else:
                warnings.append(["pc.warn.poe_off", {"name": name, "devices": names or "-", "confirm": token}])
        if "name" in after:
            warnings.append(["pc.warn.rename", {"name": name, "new": after["name"] or str(port.get("idx") or "")}])
    why = plan_lockout(state, steps)
    if why:
        blocks.append([why, {}])
    return {"steps": steps, "changes": changes, "warnings": warnings, "blocks": blocks, "confirm": confirm}


def info(state: dict, guard: dict = None) -> dict:
    """What the port cards of the web app need: per port why its networks and power cannot change (a key, or absent),
    the devices on it, the profiles to choose from."""
    out = {"locked": {}, "devices": {}, "profiles": [{"id": p["id"], "name": p.get("name") or "?"} for p in state.get("port_profiles") or []
                                                     if isinstance(p, dict) and p.get("id")]}
    for p in state.get("ports") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        why = infra_reason(state, p)
        if not why:
            probe = {"kind": "port.cycle", "object": p["id"], "before": {}, "after": {"cycle": True}}
            why = step_lockout(state, probe, guard)
        if why:
            out["locked"][p["id"]] = why
        devs = devices_on(state, p)
        if devs:
            out["devices"][p["id"]] = [{"id": d["id"], "name": d.get("name") or "?", "kind": d.get("kind")} for d in devs[:8]]
    return out
