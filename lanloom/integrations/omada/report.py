"""Night report contribution of the Omada family (pure, no I/O, no clock of its own).

``night_section(previous, state, now, name)`` compares the neutral state at the last report with the state now and
lists what deserves a look in the morning, in plain words (every text is an i18n key with parameters; no internal
ids). Registered in the family manifest (``report``); the night report shows the section only when the family's read
half delivered a state - no source, no section.

Items, most important first:
* the adapter's findings (``fwr.finding.<kind>``: a device is offline, an internet connection is down, a wireless
  network without encryption, rules switched off);
* ``night.ctl.device_down`` / ``night.ctl.device_up``  a device went offline or came back since the last report;
* ``night.ctl.device_new`` / ``night.ctl.device_gone``  devices adopted or forgotten;
* ``night.ctl.firmware``  a device runs another firmware than at the last report;
* ``night.ctl.ssid_new`` / ``night.ctl.ssid_gone`` / ``night.ctl.ssid_changed``  wireless networks added, removed, or
  changed (security, VLAN, bands, hidden);
* ``night.ctl.acl_new`` / ``night.ctl.acl_gone`` / ``night.ctl.acl_changed`` / ``night.ctl.acl_on`` /
  ``night.ctl.acl_off``  access control rules added, removed, acting differently, switched on or off.

Product names are used only to identify compatible products; no affiliation or endorsement.
"""

LEVELS = {"alert": 0, "warn": 1, "info": 2}
MAX_ITEMS = 40


def _item(level: str, key: str, **params) -> dict:
    return {"level": level, "key": key, "params": {k: v for k, v in params.items() if v is not None}}


def _is_omada(state) -> bool:
    return isinstance(state, dict) and isinstance(state.get("controller"), dict) and state["controller"].get("class") == "access_controller"


def _by_id(rows) -> dict:
    return {r["id"]: r for r in rows or [] if isinstance(r, dict) and r.get("id")}


def _acls(state) -> dict:
    return {r["ref"]: r for r in state.get("acl_digest") or [] if isinstance(r, dict) and r.get("ref")}


def _ssid_sig(s) -> tuple:
    return (s.get("security"), s.get("vlan"), tuple(s.get("bands") or ()), bool(s.get("hidden")), bool(s.get("guest")))


def night_section(previous, state, now: float, name: str = None):
    """The section, or None when there is no state (no source, no section)."""
    if not _is_omada(state):
        return None
    prev = previous if _is_omada(previous) else None
    eq, ssids, acls = _by_id(state.get("equipment")), _by_id(state.get("ssids")), _acls(state)
    went_down = set()
    if prev is not None:
        eq_before = _by_id(prev.get("equipment"))
        went_down = {eid for eid, e in eq.items() if (eq_before.get(eid) or {}).get("online") and not e.get("online") and not e.get("pending")}
    items = []
    for f in state.get("findings") or []:
        if f.get("kind") == "device_offline" and f.get("subject") in went_down:
            continue                                    # said once, as the change since the last report
        items.append(_item(f.get("level") if f.get("level") in LEVELS else "info", "fwr.finding.%s" % f.get("kind"),
                           **(f.get("params") or {})))
    changes = 0
    if prev is not None:
        eq0, ssids0, acls0 = _by_id(prev.get("equipment")), _by_id(prev.get("ssids")), _acls(prev)
        for eid in sorted(eq, key=lambda k: str(eq[k].get("name"))):
            e, old = eq[eid], eq0.get(eid)
            if old is None:
                changes += 1
                items.append(_item("info", "night.ctl.device_new", device=e.get("name")))
                continue
            if old.get("online") and not e.get("online") and not e.get("pending"):
                changes += 1
                items.append(_item("warn", "night.ctl.device_down", device=e.get("name")))
            elif not old.get("online") and e.get("online"):
                changes += 1
                items.append(_item("info", "night.ctl.device_up", device=e.get("name")))
            if old.get("firmware") and e.get("firmware") and old["firmware"] != e["firmware"]:
                changes += 1
                items.append(_item("info", "night.ctl.firmware", device=e.get("name"), before=old["firmware"], now=e["firmware"]))
        for eid in sorted(set(eq0) - set(eq), key=str):
            changes += 1
            items.append(_item("info", "night.ctl.device_gone", device=eq0[eid].get("name")))
        for sid in sorted(ssids, key=lambda k: str(ssids[k].get("name"))):
            s, old = ssids[sid], ssids0.get(sid)
            if old is None:
                changes += 1
                items.append(_item("warn" if s.get("security") == "open" else "info", "night.ctl.ssid_new", ssid=s.get("name")))
            elif _ssid_sig(old) != _ssid_sig(s):
                changes += 1
                items.append(_item("warn", "night.ctl.ssid_changed", ssid=s.get("name")))
        for sid in sorted(set(ssids0) - set(ssids), key=str):
            changes += 1
            items.append(_item("info", "night.ctl.ssid_gone", ssid=ssids0[sid].get("name")))
        for ref in sorted(acls, key=lambda k: str(acls[k].get("name"))):
            r, old = acls[ref], acls0.get(ref)
            if old is None:
                changes += 1
                items.append(_item("warn" if r.get("allow") and r.get("enabled") else "info", "night.ctl.acl_new", rule=r.get("name")))
            elif bool(old.get("enabled")) != bool(r.get("enabled")):
                changes += 1
                items.append(_item("info", "night.ctl.acl_on" if r.get("enabled") else "night.ctl.acl_off", rule=r.get("name")))
            elif old.get("sig") != r.get("sig"):
                changes += 1
                items.append(_item("warn", "night.ctl.acl_changed", rule=r.get("name")))
        for ref in sorted(set(acls0) - set(acls), key=str):
            changes += 1
            items.append(_item("info", "night.ctl.acl_gone", rule=acls0[ref].get("name")))
    items.sort(key=lambda i: LEVELS[i["level"]])
    look = sum(1 for i in items if i["level"] in ("alert", "warn"))
    online = sum(1 for e in eq.values() if e.get("online"))
    return {"family": "omada", "title": "night.ctl.title", "name": name, "look": look,
            "summary": "night.ctl.summary_look" if look else "night.ctl.summary_calm",
            "params": {"n": look, "devices": len(eq), "online": online, "ssids": len(ssids), "rules": len(acls), "changes": changes},
            "items": items[:MAX_ITEMS]}
