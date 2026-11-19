"""Night report contribution of the MikroTik family (pure, no I/O, no clock of its own).

``night_section(previous, state, now, name)`` compares the neutral state at the last report with the state now and
lists what deserves a look in the morning, in plain words (every text is an i18n key with parameters; no internal
ids). Registered in the family manifest (``report``); the night report shows the section only when the family's read
half delivered a state - no source, no section.

Items, most important first:
* the adapter's findings (``fwr.finding.<kind>``: an internet connection is down, no drop rule on the input chain,
  rules switched off);
* ``night.rtr.router_restarted``  the router's uptime is shorter than at the last report (it started again);
* ``night.rtr.firmware``  the router runs another RouterOS release than at the last report;
* ``night.rtr.neighbour_new`` / ``night.rtr.neighbour_gone``  network equipment that appeared or vanished from its
  neighbour list;
* ``night.rtr.wan_down`` / ``night.rtr.wan_up``  an internet connection went down or came back;
* ``night.rtr.rule_new`` / ``night.rtr.rule_gone`` / ``night.rtr.rule_changed`` / ``night.rtr.rule_on`` /
  ``night.rtr.rule_off``  filter rules added, removed, acting differently, switched on or off;
* ``night.rtr.forward_new`` / ``night.rtr.forward_gone``  port forwarding added or removed;
* ``night.rtr.hosts_new``  devices the router had not known at the last report (a note, with the first names).

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
from .model import CONTROLLER_CLASS

LEVELS = {"alert": 0, "warn": 1, "info": 2}
MAX_ITEMS = 40
NAMES_SHOWN = 3


def _item(level: str, key: str, **params) -> dict:
    return {"level": level, "key": key, "params": {k: v for k, v in params.items() if v is not None}}


def _is_mine(state) -> bool:
    return isinstance(state, dict) and isinstance(state.get("controller"), dict) and state["controller"].get("class") == CONTROLLER_CLASS


def _by_id(rows) -> dict:
    return {r["id"]: r for r in rows or [] if isinstance(r, dict) and r.get("id")}


def _rules(state) -> dict:
    return {r["ref"]: r for r in state.get("rule_digest") or [] if isinstance(r, dict) and r.get("ref")}


def _router(state):
    eq = state.get("equipment") or []
    return eq[0] if eq else {}


def night_section(previous, state, now: float, name: str = None):
    """The section, or None when there is no state (no source, no section)."""
    if not _is_mine(state):
        return None
    prev = previous if _is_mine(previous) else None
    items = []
    went_down = set()
    if prev is not None:
        wans0 = _by_id(prev.get("wans"))
        went_down = {w["id"] for w in state.get("wans") or [] if (wans0.get(w["id"]) or {}).get("up") and w.get("up") is False}
    for f in state.get("findings") or []:
        if f.get("kind") == "wan_down" and f.get("subject") in went_down:
            continue                                    # said once, as the change since the last report
        items.append(_item(f.get("level") if f.get("level") in LEVELS else "info", "fwr.finding.%s" % f.get("kind"),
                           **(f.get("params") or {})))
    changes = 0
    rules, fwds = _rules(state), _by_id(state.get("port_forwards"))
    nb = {e["id"]: e for e in (state.get("equipment") or [])[1:]}
    if prev is not None:
        r0, r1 = _router(prev), _router(state)
        up0, up1 = (r0.get("stats") or {}).get("uptime_s"), (r1.get("stats") or {}).get("uptime_s")
        if isinstance(up0, (int, float)) and isinstance(up1, (int, float)) and up1 + 60 < up0:
            changes += 1
            items.append(_item("warn", "night.rtr.router_restarted", device=r1.get("name")))
        if r0.get("firmware") and r1.get("firmware") and r0["firmware"] != r1["firmware"]:
            changes += 1
            items.append(_item("info", "night.rtr.firmware", device=r1.get("name"), before=r0["firmware"], now=r1["firmware"]))
        wans0, wans1 = _by_id(prev.get("wans")), _by_id(state.get("wans"))
        for wid in sorted(wans1, key=lambda k: str(wans1[k].get("name"))):
            w, old = wans1[wid], wans0.get(wid)
            if old is not None and old.get("up") and w.get("up") is False:
                changes += 1
                items.append(_item("alert", "night.rtr.wan_down", wan=w.get("name")))
            elif old is not None and old.get("up") is False and w.get("up"):
                changes += 1
                items.append(_item("info", "night.rtr.wan_up", wan=w.get("name")))
        nb0 = {e["id"]: e for e in (prev.get("equipment") or [])[1:]}
        for eid in sorted(nb, key=lambda k: str(nb[k].get("name"))):
            if eid not in nb0:
                changes += 1
                items.append(_item("info", "night.rtr.neighbour_new", device=nb[eid].get("name")))
        for eid in sorted(set(nb0) - set(nb), key=str):
            changes += 1
            items.append(_item("warn", "night.rtr.neighbour_gone", device=nb0[eid].get("name")))
        rules0, fwds0 = _rules(prev), _by_id(prev.get("port_forwards"))
        for ref in sorted(rules, key=lambda k: str(rules[k].get("name"))):
            r, old = rules[ref], rules0.get(ref)
            if old is None:
                changes += 1
                items.append(_item("warn" if r.get("allow") and r.get("enabled") else "info", "night.rtr.rule_new", rule=r.get("name")))
            elif bool(old.get("enabled")) != bool(r.get("enabled")):
                changes += 1
                items.append(_item("info", "night.rtr.rule_on" if r.get("enabled") else "night.rtr.rule_off", rule=r.get("name")))
            elif old.get("sig") != r.get("sig"):
                changes += 1
                items.append(_item("warn", "night.rtr.rule_changed", rule=r.get("name")))
        for ref in sorted(set(rules0) - set(rules), key=str):
            changes += 1
            items.append(_item("info", "night.rtr.rule_gone", rule=rules0[ref].get("name")))
        for fid in sorted(fwds, key=lambda k: str(fwds[k].get("name"))):
            if fid not in fwds0 and fwds[fid].get("enabled"):
                changes += 1
                items.append(_item("warn", "night.rtr.forward_new", rule=fwds[fid].get("name")))
        for fid in sorted(set(fwds0) - set(fwds), key=str):
            changes += 1
            items.append(_item("info", "night.rtr.forward_gone", rule=fwds0[fid].get("name")))
        known = set(prev.get("known_hosts") or []) if prev.get("known_hosts") is not None else None
        new = [d for d in state.get("devices") or [] if d["id"] not in known] if known is not None else []
        if new:
            names = [d.get("name") for d in new if d.get("name") and d["name"] != "?"][:NAMES_SHOWN]
            items.append(_item("info", "night.rtr.hosts_new", n=len(new), names=", ".join(names) if names else None))
    items.sort(key=lambda i: LEVELS[i["level"]])
    look = sum(1 for i in items if i["level"] in ("alert", "warn"))
    router = _router(state)
    wans = state.get("wans") or []
    return {"family": "mikrotik", "title": "night.rtr.title", "name": name, "look": look,
            "summary": "night.rtr.summary_look" if look else "night.rtr.summary_calm",
            "params": {"n": look, "clients": sum(1 for d in state.get("devices") or [] if d.get("online")),
                       "wans": len(wans), "wans_up": sum(1 for w in wans if w.get("up")), "rules": len(rules),
                       "forwards": len(fwds), "changes": changes},
            "items": items[:MAX_ITEMS]}
