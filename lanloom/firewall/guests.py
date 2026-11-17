"""Lockout rules for guest changes on a hypervisor (stage B), on the neutral model. Pure functions, no I/O.

Used twice, with the same rules: by the web app when it builds a preview (with what the app knows about the network)
and by the executor's write half right before it writes (with what the executor sees on its own). A refusal on either
side stops the plan.

A guest is **critical** when stopping it, moving one of its network cards or switching its firewall on could cut off
what keeps the network or Lanloom itself running:

* it runs Lanloom or the executor (one of its addresses or hardware addresses is the app's or the executor's own);
* it is the network's name server, its gateway or a controller/hub Lanloom reads (addresses the caller lists);
* it carries the tag ``lanloom-protect`` or is listed in the executor's options (``protect_guests``).

Guests with the hypervisor's own protection flag are refused by the neutral rules already (``protected``).

What a critical guest may still do: start, and switch its firewall off. Nothing that can take it off the network.
"""
from . import steps as stepkinds

PROTECT_TAG = "lanloom-protect"
REASONS = ("hv.why.self", "hv.why.executor", "hv.why.dns", "hv.why.gateway", "hv.why.controller", "hv.why.hub",
           "hv.why.tag", "hv.why.listed")
BRIDGE_KINDS = ("bridge", "ovs_bridge")


def _by_id(state: dict, area: str) -> dict:
    return {o["id"]: o for o in state.get(area) or [] if isinstance(o, dict) and "id" in o}


def critical_guests(state: dict, addresses=None, mac_hashes=None, vmids=(), tag: str = PROTECT_TAG) -> dict:
    """{guest id: reason key} for every critical guest.

    ``addresses``: {address: reason key}; ``mac_hashes``: {hash of a hardware address (``model.mac_hash``): reason key}
    (a guest without a guest agent shows no address, but its network card's hardware address is known);
    ``vmids``: guest numbers that are always critical (executor options)."""
    addresses = dict(addresses or {})
    mac_hashes = dict(mac_hashes or {})
    listed = {int(v) for v in vmids or () if isinstance(v, int) and not isinstance(v, bool) or str(v).isdigit()}
    nics = {}
    for n in state.get("guest_nics") or []:
        nics.setdefault(n.get("guest"), []).append(n)
    out = {}
    for g in state.get("guests") or []:
        why = None
        for ip in g.get("ips") or []:
            if ip in addresses:
                why = addresses[ip]
                break
        if why is None:
            for n in nics.get(g["id"], []):
                if n.get("mac_hash") and n["mac_hash"] in mac_hashes:
                    why = mac_hashes[n["mac_hash"]]
                    break
                for ip in n.get("ips") or []:
                    if ip in addresses:
                        why = addresses[ip]
                        break
                if why:
                    break
        if why is None and tag and tag in (g.get("tags") or []):
            why = "hv.why.tag"
        if why is None and g.get("vmid") in listed:
            why = "hv.why.listed"
        if why is not None:
            out[g["id"]] = why if why in REASONS else "hv.why.self"
    return out


def target_bridges(state: dict, guest: dict) -> dict:
    """{name: {"vlan_aware", "vids", "vnet"}} - where a network card of this guest may be attached: bridges of its own
    node and virtual networks of the software-defined network."""
    out = {}
    for b in state.get("bridges") or []:
        if b.get("host") == guest.get("host") and b.get("kind") in BRIDGE_KINDS and b.get("active", True) is not False:
            out[b["name"]] = {"vlan_aware": bool(b.get("vlan_aware")), "vids": b.get("vids"), "vnet": False}
    for v in state.get("sdn_vnets") or []:
        if v.get("name") and v["name"] not in out:
            out[v["name"]] = {"vlan_aware": bool(v.get("vlan_aware")), "vids": None, "vnet": True}
    return out


def vids_allow(vids, vlan) -> bool:
    """Is ``vlan`` inside a bridge's allowed list (``2-4094`` or ``10 20 30-40``)? No list = every VLAN."""
    if not vids or vlan is None:
        return True
    for part in str(vids).replace(",", " ").replace(";", " ").split():
        a, _, b = part.partition("-")
        try:
            lo, hi = int(a), int(b or a)
        except ValueError:
            continue
        if lo <= vlan <= hi:
            return True
    return False


def step_lockout(state: dict, step: dict, critical: dict):
    """A refusal key for one guest step, or None. ``critical``: the result of ``critical_guests``."""
    kind = step.get("kind")
    if kind not in ("guest.power", "guest.firewall", "guest.nic", "guest.fw_rule", "guest.fw_rule_create", "guest.fw_rule_delete"):
        return None
    guests = _by_id(state, "guests")
    after = step.get("after") or {}
    if kind in ("guest.power", "guest.firewall"):
        g = guests.get(step.get("object"))
    elif kind == "guest.nic":
        nic = _by_id(state, "guest_nics").get(step.get("object")) or {}
        g = guests.get(nic.get("guest"))
    elif kind == "guest.fw_rule_create":
        g = guests.get(after.get("guest"))
    else:
        rule = _by_id(state, "guest_rules").get(step.get("object")) or {}
        g = guests.get(rule.get("guest") or rule.get("owner"))
    if g is None:
        return "fw.block.unknown"
    if g.get("template"):
        return "fw.block.hv_template"
    if g.get("lock"):
        return "fw.block.hv_locked"
    hot = g["id"] in critical
    if kind == "guest.power":
        if g.get("ha") and g.get("ha") not in ("ignored",):
            return "fw.block.hv_ha"                     # the cluster's HA manager starts and stops this guest itself
        if after.get("status") == "stopped" and hot:
            return "fw.block.hv_critical"
        if after.get("status") == "running" and g.get("status") not in ("stopped",):
            return "fw.block.hv_state"                  # paused or suspended guests are resumed on the host, not here
        return None
    if kind == "guest.firewall":
        return "fw.block.hv_critical" if hot and after.get("firewall") else None
    if hot:
        return "fw.block.hv_critical"
    if kind == "guest.nic":
        targets = target_bridges(state, g)
        bridge = after.get("bridge", nic.get("bridge"))
        if "bridge" in after and bridge not in targets:
            return "fw.block.hv_bridge_unknown"
        tb = targets.get(bridge) or {}
        vlan = after.get("vlan", nic.get("vlan"))
        if vlan is not None and tb.get("vnet") and not tb.get("vlan_aware"):
            return "fw.block.hv_vlan_vnet"
        if vlan is not None and tb.get("vlan_aware") and not vids_allow(tb.get("vids"), vlan):
            return "fw.block.hv_vlan_not_allowed"
        return None
    if kind == "guest.fw_rule":
        rule = _by_id(state, "guest_rules").get(step.get("object")) or {}
        if not rule.get("plain") and set(stepkinds.changed_fields(step)) - {"enabled"}:
            return "fw.block.hv_rule_complex"
    return None


def plan_lockout(state: dict, steps: list):
    """Rules over a whole plan: one change per guest object, and a new or removed rule is the only rule change of its
    guest in that plan (rule positions move when a rule comes or goes)."""
    seen_guest_obj = set()
    rule_steps = {}
    rules = _by_id(state, "guest_rules")
    for st in steps:
        kind = st.get("kind")
        if kind in ("guest.power", "guest.firewall"):
            if st.get("object") in seen_guest_obj:
                return "fw.block.hv_one_per_guest"
            seen_guest_obj.add(st.get("object"))
        if kind in ("guest.fw_rule", "guest.fw_rule_create", "guest.fw_rule_delete"):
            guest = (st.get("after") or {}).get("guest") if kind == "guest.fw_rule_create" else \
                (rules.get(st.get("object")) or {}).get("guest")
            rule_steps.setdefault(guest, []).append(kind)
    for kinds in rule_steps.values():
        if len(kinds) > 1 and any(k != "guest.fw_rule" for k in kinds):
            return "fw.block.hv_rule_mix"
    return None


# ---------------------------------------------------------------------------- drafts and preview (web app)
GUEST_KINDS = ("guest.power", "guest.firewall", "guest.nic", "guest.fw_rule", "guest.fw_rule_create", "guest.fw_rule_delete")
RULE_DEFAULTS = {"name": "", "proto": "any", "ports": "", "source": "", "enabled": True}
FIELD_ORDER = ("status", "firewall", "bridge", "vlan", "direction", "action", "proto", "ports", "source", "name", "enabled")


def _cur(obj: dict, field: str, sample):
    v = obj.get(field)
    return "" if v is None and isinstance(sample, str) else v


def op_key(kind: str, obj: str) -> str:
    return "%s|%s" % (kind, obj)


def clean_ops(state: dict, ops) -> dict:
    """Draft guest changes {key: {"kind", "object", "after"}} reduced to valid, real changes, each with ``before`` read
    from ``state``: unknown objects, unknown fields, invalid values and values equal to the current ones are dropped.
    A new rule gets a fresh ``new:`` id; a removal carries the whole rule as ``before``."""
    import secrets
    out = {}
    for op in (ops.values() if isinstance(ops, dict) else ops if isinstance(ops, list) else []):
        if not isinstance(op, dict) or op.get("kind") not in GUEST_KINDS or not isinstance(op.get("after", {}), dict):
            continue
        kind = stepkinds.KINDS[op["kind"]]
        after = dict(op.get("after") or {})
        obj_id = op.get("object")
        if kind.verb == "create":
            if not isinstance(obj_id, str) or not stepkinds.NEW_ID.match(obj_id):
                obj_id = "new:" + secrets.token_hex(8)
            if after.get("guest") not in _by_id(state, "guests"):
                continue
            full = dict(RULE_DEFAULTS, **{k: v for k, v in after.items() if k in kind.fields})
            if (full.get("proto") not in ("tcp", "udp")) and full.get("ports"):
                full["ports"] = ""
            step = {"kind": kind.name, "object": obj_id, "before": {}, "after": full}
        elif kind.verb == "delete":
            rule = _by_id(state, "guest_rules").get(obj_id)
            if not rule or not rule.get("own"):
                continue
            before = {f: _cur(rule, f, RULE_DEFAULTS.get(f, "")) for f in kind.fields if f != "guest"}
            before["guest"] = rule.get("guest") or rule.get("owner")
            step = {"kind": kind.name, "object": obj_id, "before": before, "after": {}}
        else:
            obj = _by_id(state, kind.area).get(obj_id)
            if not obj:
                continue
            keep = {}
            for f, v in after.items():
                if f not in kind.fields:
                    continue
                if isinstance(v, str):
                    v = v.strip()
                if v != _cur(obj, f, v):
                    keep[f] = v
            if not keep:
                continue
            step = {"kind": kind.name, "object": obj_id, "before": {f: _cur(obj, f, v) for f, v in keep.items()}, "after": keep}
        try:
            stepkinds.check_step(step)
        except stepkinds.FieldError:
            continue
        out[op_key(kind.name, obj_id)] = step
    return out


def _label(state: dict, step: dict) -> tuple:
    """(guest, display name of the object) for a guest step."""
    guests = _by_id(state, "guests")
    kind = step["kind"]
    if kind in ("guest.power", "guest.firewall"):
        g = guests.get(step["object"]) or {}
        return g, g.get("name") or "?"
    if kind == "guest.nic":
        nic = _by_id(state, "guest_nics").get(step["object"]) or {}
        g = guests.get(nic.get("guest")) or {}
        return g, "%s · %s" % (g.get("name") or "?", nic.get("name") or "?")
    if kind == "guest.fw_rule_create":
        g = guests.get(step["after"].get("guest")) or {}
        return g, g.get("name") or "?"
    rule = _by_id(state, "guest_rules").get(step["object"]) or {}
    g = guests.get(rule.get("guest") or rule.get("owner")) or {}
    return g, g.get("name") or "?"


def preview(state: dict, ops: dict, critical: dict) -> dict:
    """Steps, plain-language changes, warnings (with the confirmation token of ``B*`` steps), blocks and confirmations
    for drafted guest changes. ``ops``: the result of ``clean_ops``; ``critical``: the result of ``critical_guests``."""
    from . import model
    steps, changes, warnings, blocks, confirm = [], [], [], [], []
    nics = _by_id(state, "guest_nics")
    rules = _by_id(state, "guest_rules")
    for key in sorted(ops):
        st = ops[key]
        g, name = _label(state, st)
        why = model.lockout_reason(state, st) if st["kind"] != "guest.fw_rule_create" else None
        why = why or step_lockout(state, st, critical)
        if not why and st["kind"] in ("guest.fw_rule", "guest.fw_rule_create"):
            merged = dict(rules.get(st["object"]) or {}, **st["after"])
            if merged.get("ports") and merged.get("proto") not in ("tcp", "udp"):
                why = "fw.block.hv_rule_ports"
        if why:
            reason = critical.get(g.get("id")) or (g.get("protected") if why == "fw.block.protected" else "")
            blocks.append([why, {"name": name, "reason": reason or ""}])
            continue
        steps.append(st)
        kind = st["kind"]
        order = lambda kv: (FIELD_ORDER.index(kv[0]) if kv[0] in FIELD_ORDER else 99, kv[0])
        if kind == "guest.fw_rule_create":
            edit = [{"field": f, "from": "", "to": v} for f, v in sorted(st["after"].items(), key=order) if f != "guest" and v not in ("", None)]
        elif kind == "guest.fw_rule_delete":
            edit = [{"field": f, "from": v, "to": ""} for f, v in sorted(st["before"].items(), key=order) if f != "guest" and v not in ("", None)]
        else:
            edit = [{"field": f, "from": st["before"].get(f), "to": v} for f, v in sorted(st["after"].items(), key=order)]
        changes.append({"device": st["object"], "name": name, "kind": kind, "edit": edit,
                        "what": {"guest.fw_rule_create": "hv.change.rule_new", "guest.fw_rule_delete": "hv.change.rule_gone",
                                 "guest.fw_rule": "hv.change.rule", "guest.nic": "hv.change.nic",
                                 "guest.power": "hv.change.start" if st["after"].get("status") == "running" else "hv.change.shutdown",
                                 "guest.firewall": "hv.change.firewall"}[kind]})
        token = stepkinds.confirm_token(st) if stepkinds.needs_confirmation(st) else None
        if token:
            confirm.append(token)
        if kind == "guest.power":
            warnings.append(["fw.warn.hv_stop" if st["after"]["status"] == "stopped" else "fw.warn.hv_start",
                             {"name": name, "confirm": token}])
        elif kind == "guest.firewall":
            pol = next((o.get("policy_in") for o in state.get("fw_options") or [] if o.get("scope") == "guest" and o.get("owner") == g.get("id")), None)
            warnings.append(["fw.warn.hv_fw_on" if st["after"]["firewall"] else "fw.warn.hv_fw_off",
                             {"name": name, "policy": (pol or "ACCEPT").upper(), "confirm": token}])
        elif kind == "guest.nic":
            nic = nics.get(st["object"]) or {}
            if g.get("status") == "running" and ({"bridge", "vlan"} & set(st["after"])):
                warnings.append(["fw.warn.hv_nic", {"name": name}])
            tb = target_bridges(state, g).get(st["after"].get("bridge", nic.get("bridge"))) or {}
            vlan = st["after"].get("vlan", nic.get("vlan"))
            if vlan is not None and not tb.get("vlan_aware") and not tb.get("vnet"):
                warnings.append(["fw.warn.hv_vlan_classic", {"name": name, "bridge": st["after"].get("bridge", nic.get("bridge")) or "?"}])
        elif kind in ("guest.fw_rule", "guest.fw_rule_create"):
            if g.get("firewall") is False:
                warnings.append(["fw.warn.hv_rules_inactive", {"name": name}])
    why = plan_lockout(state, steps)
    if why:
        blocks.append([why, {}])
    return {"steps": steps, "changes": changes, "warnings": warnings, "blocks": blocks, "confirm": confirm}
