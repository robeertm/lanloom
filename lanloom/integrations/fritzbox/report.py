"""Night report contribution of the FRITZ!Box family (pure, no I/O, no clock of its own).

``night_section(previous, state, now, name)`` compares the neutral state at the last report with the state now and
lists what deserves a look in the morning, in plain words (every text is an i18n key with parameters; no internal
ids). Registered in the family manifest (``report``); the night report shows the section only when the family's read
half delivered a state - no source, no section.

Items, most important first:
* the adapter's findings (``fwr.finding.<kind>``: the internet connection is down, a mesh device is offline);
* ``night.home.device_down`` / ``night.home.device_up``  a mesh device went offline or came back since the last report;
* ``night.home.device_new`` / ``night.home.device_gone``  mesh devices that joined or left;
* ``night.home.firmware``  the box or a mesh device runs another firmware than at the last report;
* ``night.home.hosts_new``  devices the box had not known at the last report (a note, with the first names).

Product names are used only to identify compatible products; no affiliation or endorsement.
"""

LEVELS = {"alert": 0, "warn": 1, "info": 2}
MAX_ITEMS = 40
NAMES_SHOWN = 3


def _item(level: str, key: str, **params) -> dict:
    return {"level": level, "key": key, "params": {k: v for k, v in params.items() if v is not None}}


def _is_home(state) -> bool:
    return isinstance(state, dict) and isinstance(state.get("controller"), dict) and state["controller"].get("class") == "home_router"


def _by_id(rows) -> dict:
    return {r["id"]: r for r in rows or [] if isinstance(r, dict) and r.get("id")}


def night_section(previous, state, now: float, name: str = None):
    """The section, or None when there is no state (no source, no section)."""
    if not _is_home(state):
        return None
    prev = previous if _is_home(previous) else None
    eq, hosts = _by_id(state.get("equipment")), _by_id(state.get("devices"))
    went_down = set()
    if prev is not None:
        eq_before = _by_id(prev.get("equipment"))
        went_down = {eid for eid, e in eq.items() if (eq_before.get(eid) or {}).get("online") and not e.get("online")}
    items = []
    for f in state.get("findings") or []:
        if f.get("kind") == "mesh_device_offline" and f.get("subject") in went_down:
            continue                                    # said once, as the change since the last report
        items.append(_item(f.get("level") if f.get("level") in LEVELS else "info", "fwr.finding.%s" % f.get("kind"),
                           **(f.get("params") or {})))
    changes = 0
    if prev is not None:
        eq0 = _by_id(prev.get("equipment"))
        for eid in sorted(eq, key=lambda k: str(eq[k].get("name"))):
            e, old = eq[eid], eq0.get(eid)
            if old is None:
                changes += 1
                items.append(_item("info", "night.home.device_new", device=e.get("name")))
                continue
            if old.get("online") and not e.get("online"):
                changes += 1
                items.append(_item("warn", "night.home.device_down", device=e.get("name")))
            elif not old.get("online") and e.get("online"):
                changes += 1
                items.append(_item("info", "night.home.device_up", device=e.get("name")))
            if old.get("firmware") and e.get("firmware") and old["firmware"] != e["firmware"]:
                changes += 1
                items.append(_item("info", "night.home.firmware", device=e.get("name"), before=old["firmware"], now=e["firmware"]))
        for eid in sorted(set(eq0) - set(eq), key=str):
            changes += 1
            items.append(_item("info", "night.home.device_gone", device=eq0[eid].get("name")))
        known = set(prev.get("known_hosts") or []) if prev.get("known_hosts") is not None else None
        fresh = sorted((hosts[h] for h in set(hosts) - known), key=lambda d: str(d.get("name"))) if known is not None else []
        if fresh:
            changes += len(fresh)
            names = ", ".join(str(d.get("name") or "?") for d in fresh[:NAMES_SHOWN]) + (" …" if len(fresh) > NAMES_SHOWN else "")
            items.append(_item("info", "night.home.hosts_new", n=len(fresh), names=names))
    items.sort(key=lambda i: LEVELS[i["level"]])
    look = sum(1 for i in items if i["level"] in ("alert", "warn"))
    return {"family": "fritzbox", "title": "night.home.title", "name": name, "look": look,
            "summary": "night.home.summary_look" if look else "night.home.summary_calm",
            "params": {"n": look, "devices": len(eq), "online": sum(1 for e in eq.values() if e.get("online")),
                       "hosts": sum(1 for d in hosts.values() if d.get("online")), "changes": changes},
            "items": items[:MAX_ITEMS]}
