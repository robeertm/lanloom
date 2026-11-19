"""Wireless networks (SSIDs) and blocked clients on the neutral model (stage B). Pure functions, no I/O.

Used twice, with the same rules: by the web app when it builds a preview and by the executor's write half right before
it writes (with its own fresh read). A refusal on either side stops the plan.

What may be written here, and what never:

* **SSIDs** - create one, change its name, network, bands, security, hidden flag, switch it off and on; delete only an
  SSID Lanloom created itself. Radio settings of the access points (channel, channel width, transmit power, the band
  plan of a radio) are never written: they belong to the access point, not to the wireless network, and are only shown.
* **never: the own path** - a change that drops the clients of an SSID (off, deleted, renamed, another network, other
  bands, other security) is refused while a device Lanloom protects (gateway, server, hub, Lanloom's own host), the
  approver's device, the executor or the controller is connected over it.
* **networks** - an SSID carries only a local network (no internet uplink, no VPN). The management network needs its
  own confirmation (whoever joins then reaches the admin pages of the network equipment); an open SSID there is
  refused. An open SSID (no password) only on a guest network, and only with its own confirmation. 6 GHz only with
  WPA3 (the band allows nothing else).
* **passwords** - never part of a plan, a preview, the history or a log. A new protected SSID (or an open one that gets
  a password) gets a long random password from the executor; it is read or changed in the controller's own app.
* **clients** - block and unblock a client (always with its own confirmation, ``B*``). Never a device Lanloom protects,
  the approver's device, the executor, the controller or Lanloom's own host. Disconnecting a client ("kick") is no
  step: it reconnects at once, so it is never offered as a lasting answer.

The answers of ``step_lockout`` are refusal keys (``ws.block.*`` or the shared ``fw.block.*``); ``preview`` adds the
plain warnings (``ws.warn.*``) and the confirmation tokens.
"""
import copy

from . import steps as stepkinds

SSID_KINDS = ("ssid.create", "ssid.update", "ssid.delete")
CLIENT_KINDS = ("client.block",)
WIRELESS_KINDS = SSID_KINDS + CLIENT_KINDS
FIELD_ORDER = ("name", "network", "bands", "security", "hidden", "enabled", "blocked")
LOCAL_PURPOSES = ("lan", "iot", "guest", "", None)       # networks an SSID may carry; never internet uplinks or VPNs
SECURITY = ("open", "wpa2_personal", "wpa2_wpa3_personal", "wpa3_personal")
BANDS = ("2.4ghz", "5ghz", "6ghz")
SSID_DEFAULTS = {"bands": ["2.4ghz", "5ghz"], "security": "wpa2_wpa3_personal", "hidden": False, "enabled": True}
# fields whose change drops the clients of an SSID (they have to join again, or cannot)
CUTTING = ("name", "network", "bands", "security", "enabled")
MANY_SSIDS = 4                                          # more SSIDs per band cost airtime on every access point
WHAT = {"ssid.create": "ws.change.create", "ssid.update": "ws.change.update", "ssid.delete": "ws.change.delete",
        "client.block": "ws.change.block"}


# ---------------------------------------------------------------------------- small helpers
def _by_id(state: dict, area: str) -> dict:
    return {o["id"]: o for o in state.get(area) or [] if isinstance(o, dict) and "id" in o}


def ssid_of(state: dict, sid) -> dict:
    return _by_id(state, "ssids").get(sid) or {}


def label(state: dict, step: dict) -> str:
    """The SSID's or the client's name for a step (a new SSID: the name it gets)."""
    if step.get("kind") in CLIENT_KINDS:
        return (_by_id(state, "devices").get(step.get("object")) or {}).get("name") or "?"
    obj = ssid_of(state, step.get("object"))
    return obj.get("name") or (step.get("after") or {}).get("name") or (step.get("before") or {}).get("name") or "?"


def clients_on(state: dict, ssid: dict) -> list:
    """Wireless devices the controller sees on this SSID right now (by its name, as the controller reports them)."""
    name = ssid.get("name")
    if not name:
        return []
    return [d for d in state.get("devices") or [] if not d.get("wired") and d.get("ssid") == name
            and d.get("online", True) is not False]


def is_guest_network(state: dict, nid) -> bool:
    """A guest network: the gateway says so, or an SSID the controller marks as a guest network carries it."""
    if (_by_id(state, "networks").get(nid) or {}).get("purpose") == "guest":
        return True
    return any(s.get("guest") and s.get("network") == nid for s in state.get("ssids") or [] if isinstance(s, dict))


def management_networks(state: dict) -> set:
    return {n["id"] for n in state.get("networks") or [] if n.get("management")}


def _net_problem(state: dict, nid):
    net = _by_id(state, "networks").get(nid)
    if net is None:
        return "fw.block.unknown"
    if net.get("purpose") not in LOCAL_PURPOSES:
        return "fw.block.network_kind"
    return None


def merged(state: dict, step: dict) -> dict:
    """The SSID as it is after the step (a new one: its fields with the defaults)."""
    if step.get("kind") == "ssid.create":
        return dict(copy.deepcopy(SSID_DEFAULTS), **copy.deepcopy(step.get("after") or {}))
    return dict(ssid_of(state, step.get("object")), **copy.deepcopy(step.get("after") or {}))


def cuts(step: dict) -> bool:
    """Does the step drop the clients of an existing SSID? (Hidden, and switching an SSID on, never does.)"""
    kind = step.get("kind")
    if kind == "ssid.delete":
        return True
    if kind != "ssid.update":
        return False
    after, before = step.get("after") or {}, step.get("before") or {}
    for f in CUTTING:
        if f not in after:
            continue
        if f == "enabled":
            if after[f] is False:
                return True
            continue
        if f == "bands":
            if set(before.get("bands") or BANDS) - set(after["bands"] or []):
                return True
            continue
        return True
    return False


def _guarded(state: dict, devices: list, guard: dict):
    """A refusal key when one of ``devices`` must keep its way (protected, the approver's, a guarded address)."""
    guard = guard or {}
    addrs = {a for a in guard.get("addresses") or () if a}
    mine = set(guard.get("devices") or ())
    for d in devices:
        if d.get("protected"):
            return "ws.block.protected"
        if d.get("id") in mine:
            return "ws.block.self_path"
        if (d.get("ip") and d["ip"] in addrs) or (d.get("fixed_ip") and d["fixed_ip"] in addrs):
            return "ws.block.self_path"
    return None


# ---------------------------------------------------------------------------- the rules
def step_lockout(state: dict, step: dict, guard: dict = None):
    """A refusal key for one SSID or client step, or None. ``guard``: {"addresses": addresses whose way must stay open
    (Lanloom, the executor, the controller, the gateway, the approver), "devices": device ids that must keep their way
    (the approver's)}."""
    kind = step.get("kind")
    if kind in CLIENT_KINDS:
        dev = _by_id(state, "devices").get(step.get("object"))
        if dev is None:
            return "fw.block.unknown"
        if (step.get("after") or {}).get("blocked") is False:
            return None                                # unblocking never cuts anything off
        if dev.get("protected"):
            return "ws.block.protected"
        if dev.get("id") in set((guard or {}).get("devices") or ()):
            return "ws.block.self_block"              # the approver's own device
        return _guarded(state, [dev], guard)
    if kind not in SSID_KINDS:
        return None
    old = None
    if kind != "ssid.create":
        old = _by_id(state, "ssids").get(step.get("object"))
        if old is None:
            return "fw.block.unknown"
        if old.get("editable") is False:
            return "fw.block.builtin"
        if kind == "ssid.delete" and not old.get("own"):
            return "fw.block.not_own"
    after = step.get("after") or {}
    if kind != "ssid.delete":
        new = merged(state, step)
        if "name" in after or kind == "ssid.create":
            taken = {(s.get("name") or "").strip().lower() for s in state.get("ssids") or [] if s.get("id") != step.get("object")}
            if new.get("name", "").strip().lower() in taken:
                return "ws.block.name_taken"
        if "network" in after or kind == "ssid.create":
            why = _net_problem(state, new.get("network"))
            if why:
                return why
        if "security" in after or kind == "ssid.create":
            if new.get("security") not in SECURITY:
                return "ws.block.security"
        if new.get("security") == "open" and ("security" in after or "network" in after or kind == "ssid.create"):
            if new.get("network") in management_networks(state):
                return "ws.block.open_management"
            if not is_guest_network(state, new.get("network")):
                return "ws.block.open_not_guest"
        if "6ghz" in (new.get("bands") or []) and ("bands" in after or "security" in after or kind == "ssid.create") \
                and new.get("security") != "wpa3_personal":
            return "ws.block.6ghz_wpa3"
    if old is not None and cuts(step):
        why = _guarded(state, clients_on(state, old), guard)
        if why:
            return why
    return None


def block_vars(state: dict, step: dict) -> dict:
    """The words a refusal names: the SSID or client and, where one is in the way, the device."""
    if step.get("kind") in CLIENT_KINDS:
        dev = _by_id(state, "devices").get(step.get("object")) or {}
        return {"name": dev.get("name") or "?", "device": dev.get("name") or "", "reason": dev.get("protected") or ""}
    old = ssid_of(state, step.get("object"))
    devs = clients_on(state, old) if old else []
    who = next((d for d in devs if d.get("protected")), None) or (devs[0] if devs else None)
    return {"name": label(state, step), "device": (who or {}).get("name") or "", "reason": (who or {}).get("protected") or ""}


def needs_confirmation(state: dict, step: dict) -> bool:
    """Beyond the kind's own rule (blocking a client): an SSID onto the management network, and every open SSID."""
    if step.get("kind") not in ("ssid.create", "ssid.update"):
        return False
    after = step.get("after") or {}
    new = merged(state, step)
    if ("network" in after or step["kind"] == "ssid.create") and new.get("network") in management_networks(state):
        return True
    if new.get("security") == "open" and ("security" in after or step["kind"] == "ssid.create"):
        return True
    if after.get("enabled") is True and new.get("security") == "open":
        return True                                     # switching an open SSID on opens it again
    return False


def plan_lockout(state: dict, steps: list):
    """Rules over the whole plan: one change per SSID and per client."""
    seen = set()
    for st in steps:
        if st.get("kind") in WIRELESS_KINDS:
            key = ("c" if st["kind"] in CLIENT_KINDS else "s", st.get("object"))
            if key in seen:
                return "ws.block.one_per_object"
            seen.add(key)
    return None


# ---------------------------------------------------------------------------- the state after a plan
def after_state(state: dict, steps: list, created: dict = None) -> dict:
    new = copy.deepcopy(state)
    ssids = new.setdefault("ssids", [])
    devs = _by_id(new, "devices")
    for st in steps:
        kind, oid, after = st.get("kind"), st.get("object"), st.get("after") or {}
        if kind == "ssid.create":
            ssids.append(dict(copy.deepcopy(SSID_DEFAULTS), **copy.deepcopy(after), id=(created or {}).get(oid, oid), own=True))
        elif kind == "ssid.update":
            for s in ssids:
                if s.get("id") == oid:
                    s.update(copy.deepcopy(after))
        elif kind == "ssid.delete":
            ssids[:] = [s for s in ssids if s.get("id") != oid]
        elif kind == "client.block" and oid in devs:
            devs[oid]["blocked"] = bool(after.get("blocked"))
    return new


def matches(ssid: dict, after: dict) -> bool:
    """Does an SSID (freshly read) carry the values of a step's ``after``?"""
    for f, v in after.items():
        now = ssid.get(f)
        if f == "bands":
            now, v = sorted(now or []), sorted(v or [])
        if now != v:
            return False
    return True


# ---------------------------------------------------------------------------- drafts and preview (web app)
def op_key(kind: str, obj: str) -> str:
    return "%s|%s" % (kind, obj)


def _norm(field: str, v):
    if isinstance(v, str):
        v = v.strip()
    if field == "bands" and isinstance(v, (list, tuple)):
        v = sorted({str(x).strip() for x in v if isinstance(x, str) and x.strip()}, key=lambda b: BANDS.index(b) if b in BANDS else 9)
    if field in ("hidden", "enabled", "blocked") and not isinstance(v, bool):
        v = bool(v)
    return v


def _cur(obj: dict, field: str):
    v = obj.get(field)
    if field == "bands" and isinstance(v, list):
        return sorted(v, key=lambda b: BANDS.index(b) if b in BANDS else 9)
    if field in ("hidden", "blocked"):
        return bool(v)
    if field == "enabled":
        return v is not False
    return v


def _valid(check, value) -> bool:
    try:
        check(value)
        return True
    except stepkinds.FieldError:
        return False


def clean_ops(state: dict, ops) -> dict:
    """Draft SSID and client-block changes {key: {"kind", "object", "after"}} reduced to valid, real changes, each with
    ``before`` read from ``state``. A new SSID carries the defaults for what the draft leaves out."""
    out = {}
    ssids, devs = _by_id(state, "ssids"), _by_id(state, "devices")
    items = ops.values() if isinstance(ops, dict) else ops if isinstance(ops, list) else []
    for op in items:
        if not isinstance(op, dict) or op.get("kind") not in WIRELESS_KINDS or not isinstance(op.get("after", {}), dict):
            continue
        kind = stepkinds.KINDS[op["kind"]]
        oid = op.get("object")
        if op["kind"] == "client.block":
            dev = devs.get(oid)
            if dev is None or not isinstance((op.get("after") or {}).get("blocked"), bool):
                continue
            want = op["after"]["blocked"]
            if bool(dev.get("blocked")) == want:
                continue
            step = {"kind": "client.block", "object": oid, "before": {"blocked": bool(dev.get("blocked"))}, "after": {"blocked": want}}
        elif op["kind"] == "ssid.create":
            if not isinstance(oid, str) or not stepkinds.NEW_ID.match(oid):
                continue
            after = dict(copy.deepcopy(SSID_DEFAULTS), **{f: _norm(f, v) for f, v in (op.get("after") or {}).items() if f in kind.fields})
            step = {"kind": "ssid.create", "object": oid, "before": {}, "after": after}
        elif op["kind"] == "ssid.delete":
            s = ssids.get(oid)
            if s is None:
                continue
            before = {f: _cur(s, f) for f in kind.fields if _cur(s, f) is not None and _valid(kind.fields[f], _cur(s, f))}
            step = {"kind": "ssid.delete", "object": oid, "before": before, "after": {}}
        else:
            s = ssids.get(oid)
            if s is None:
                continue
            keep = {}
            for f, v in (op.get("after") or {}).items():
                if f not in kind.fields:
                    continue
                v, cur = _norm(f, v), _cur(s, f)
                if cur is None and f != "network":
                    continue
                if v != cur and _valid(kind.fields[f], v) and (cur is None or _valid(kind.fields[f], cur)):
                    keep[f] = v
            if not keep or any(_cur(s, f) is None for f in keep):
                continue
            step = {"kind": "ssid.update", "object": oid, "before": {f: _cur(s, f) for f in keep}, "after": keep}
        try:
            stepkinds.check_step(step)
        except stepkinds.FieldError:
            continue
        out[op_key(step["kind"], oid)] = step
    return out


def preview(state: dict, ops: dict, *, deps: dict = None, guard: dict = None, lockout=None) -> dict:
    """Steps, plain-language changes, warnings (with the confirmation token of confirmed steps), blocks and
    confirmations for drafted SSID and client-block changes. ``deps``: {device id: [{"by", "via"}]};
    ``guard``: see ``step_lockout``; ``lockout``: an extra family rule (step -> key or None)."""
    from . import model, netconf
    steps, changes, warnings, blocks, confirm = [], [], [], [], []
    deps = deps or {}
    nets = _by_id(state, "networks")
    order = lambda kv: (FIELD_ORDER.index(kv[0]) if kv[0] in FIELD_ORDER else 99, kv[0])
    for key in sorted(ops):
        st = ops[key]
        name = label(state, st)
        why = (model.lockout_reason(state, st) if st["kind"] != "ssid.create" else None) or step_lockout(state, st, guard) or \
            (lockout(st) if lockout else None)
        if why:
            blocks.append([why, block_vars(state, st)])
            continue
        steps.append(st)
        kind, after = st["kind"], st["after"]
        token = stepkinds.confirm_token(st) if (stepkinds.needs_confirmation(st) or needs_confirmation(state, st)) else None
        if token:
            confirm.append(token)
        if kind == "client.block":
            changes.append({"device": st["object"], "name": name, "kind": kind, "edit": [{"field": "blocked", "from": st["before"]["blocked"],
                                                                                        "to": after["blocked"]}], "what": WHAT[kind]})
            dev = _by_id(state, "devices").get(st["object"]) or {}
            if after["blocked"]:
                warnings.append(["ws.warn.block", {"name": name, "confirm": token}])
                for dep in deps.get(st["object"], []):
                    warnings.append(netconf.dep_warning(name, dep))
                if dev.get("private_address"):
                    warnings.append(["ws.warn.block_private", {"name": name}])
            else:
                warnings.append(["ws.warn.unblock", {"name": name, "confirm": token}])
            continue
        if kind == "ssid.delete":
            edit = []
        else:
            edit = [{"field": f, "from": st["before"].get(f), "to": v} for f, v in sorted(after.items(), key=order)]
        changes.append({"device": st["object"], "name": name, "kind": kind, "edit": edit, "what": WHAT[kind]})
        old = ssid_of(state, st["object"])
        devs = clients_on(state, old) if old else []
        n = len(devs)
        new = merged(state, st)
        net_name = (nets.get(new.get("network")) or {}).get("name") or "?"
        if kind == "ssid.create":
            warnings.append(["ws.warn.create", {"name": name, "net": net_name}])
            if new.get("security") != "open":
                warnings.append(["ws.warn.password_new", {"name": name}])
        elif kind == "ssid.delete":
            warnings.append(["ws.warn.delete", {"name": name, "n": n}] if n else ["ws.warn.delete_empty", {"name": name}])
        else:
            if after.get("enabled") is False:
                warnings.append(["ws.warn.off", {"name": name, "n": n}] if n else ["ws.warn.off_empty", {"name": name}])
            if after.get("enabled") is True:
                warnings.append(["ws.warn.on", {"name": name}])
            if "name" in after:
                warnings.append(["ws.warn.rename", {"name": name, "new": after["name"], "n": n}])
            if "network" in after:
                warnings.append(["ws.warn.network", {"name": name, "net": net_name, "n": n}])
                for d in devs:
                    if d.get("fixed_ip"):
                        warnings.append(["pc.warn.fixed_ip", {"name": d.get("name") or "?", "ip": d["fixed_ip"]}])
                    for dep in deps.get(d.get("id"), []):
                        warnings.append(netconf.dep_warning(d.get("name") or "?", dep))
            if "bands" in after:
                dropped = [b for b in st["before"].get("bands") or [] if b not in after["bands"]]
                on_dropped = [d for d in devs if d.get("band") in dropped]
                warnings.append(["ws.warn.bands_drop", {"name": name, "n": len(on_dropped)}] if dropped else ["ws.warn.bands", {"name": name}])
            if "security" in after:
                if after["security"] == "open":
                    pass                                # named below together with the confirmation
                elif st["before"].get("security") == "open":
                    warnings.append(["ws.warn.password_new", {"name": name}])
                else:
                    warnings.append(["ws.warn.security", {"name": name, "n": n}])
            if "hidden" in after:
                warnings.append(["ws.warn.hidden" if after["hidden"] else "ws.warn.visible", {"name": name}])
        if kind != "ssid.delete":
            if new.get("security") == "open" and ("security" in after or kind == "ssid.create" or after.get("enabled") is True):
                warnings.append(["ws.warn.open", {"name": name, "confirm": token}])
            if new.get("network") in management_networks(state) and ("network" in after or kind == "ssid.create"):
                warnings.append(["ws.warn.to_management", {"name": name, "net": net_name, "confirm": token}])
            if new.get("enabled", True) and ("bands" in after or kind == "ssid.create" or after.get("enabled") is True):
                for band in new.get("bands") or []:
                    on_band = [s for s in state.get("ssids") or [] if s.get("enabled", True) and band in (s.get("bands") or [])
                               and s.get("id") != st["object"]]
                    if len(on_band) + 1 > MANY_SSIDS:
                        warnings.append(["ws.warn.many", {"band": band.replace("ghz", " GHz"), "n": len(on_band) + 1}])
                        break
    why = plan_lockout(state, steps)
    if why:
        blocks.append([why, {}])
    return {"steps": steps, "changes": changes, "warnings": warnings, "blocks": blocks, "confirm": confirm}


def info(state: dict, guard: dict = None) -> dict:
    """What the SSID cards and the client list of the web app need: per SSID why it cannot be switched off or moved
    (a key, or absent) and how many clients use it; per client why it cannot be blocked."""
    out = {"ssids": {}, "clients": {}, "networks": [], "security": list(SECURITY), "bands": list(BANDS)}
    for s in state.get("ssids") or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        why = "fw.block.builtin" if s.get("editable") is False else _guarded(state, clients_on(state, s), guard)
        out["ssids"][s["id"]] = {"n": len(clients_on(state, s)), "locked": why or None,
                                 "editable": s.get("editable") is not False}
    for d in state.get("devices") or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        probe = {"kind": "client.block", "object": d["id"], "before": {"blocked": False}, "after": {"blocked": True}}
        why = step_lockout(state, probe, guard)
        if why:
            out["clients"][d["id"]] = why
    out["networks"] = [{"id": n["id"], "name": n.get("name") or "?", "guest": is_guest_network(state, n["id"]),
                        "management": bool(n.get("management"))}
                       for n in state.get("networks") or [] if isinstance(n, dict) and n.get("id") and _net_problem(state, n["id"]) is None]
    return out
