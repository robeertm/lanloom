"""Night report contribution of the OpenWrt family (pure, no I/O, no clock of its own).

``night_section(previous, state, now, name)`` compares the neutral state at the last report with the state now and
lists what deserves a look in the morning, in plain words (every text is an i18n key with parameters; no internal
ids). Registered in the family manifest (``report``); the night report shows the section only when the family's
read half delivered a state.

Items, most important first:
* the adapter's findings (``fwr.finding.<kind>``: management open on the internet side, open rule from the internet
  side, any to any, forward policy accept, rules switched off, an open wireless network, a WAN link that is down);
* ``night.fwr.rule_new`` / ``rule_gone`` / ``rule_changed`` / ``rule_on`` / ``rule_off``  firewall rules, zone
  forwardings and input policies added, removed, acting differently or switched since the last report;
* ``night.fwr.forward_new`` / ``forward_gone``  port forwards added or removed.

Changes of the wireless networks are reported once for every connection in the shared section *Wireless networks*
(``firewall/report.py``), not here.

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
REPORTS_RULES = True        # its night section lists rule changes itself: the section "Changed outside Lanloom" leaves them out

LEVELS = {"alert": 0, "warn": 1, "info": 2}
MAX_ITEMS = 40


def _item(level: str, key: str, **params) -> dict:
    return {"level": level, "key": key, "params": {k: v for k, v in params.items() if v is not None}}


def _rules(state) -> dict:
    return {r["name"]: r for r in state.get("rule_digest") or [] if isinstance(r, dict) and r.get("name")}


def _is_router(state) -> bool:
    return (isinstance(state, dict) and isinstance(state.get("rule_digest"), list)
            and (state.get("controller") or {}).get("class") == "router")


def night_section(previous, state, now: float, name: str = None):
    """The section, or None when there is no state (no source, no section)."""
    if not _is_router(state):
        return None
    prev = previous if _is_router(previous) else None
    items = []
    for f in state.get("findings") or []:
        items.append(_item(f.get("level") if f.get("level") in LEVELS else "info", "fwr.finding.%s" % f.get("kind"),
                           **(f.get("params") or {})))
    rules = _rules(state)
    changes = 0
    if prev is not None:
        before = _rules(prev)
        for ref in sorted(rules):
            r, old = rules[ref], before.get(ref)
            if old is None:
                changes += 1
                items.append(_item("warn" if r.get("allow") and r.get("enabled") else "info", "night.fwr.rule_new", rule=r["name"]))
            elif bool(old.get("enabled")) != bool(r.get("enabled")):
                changes += 1
                items.append(_item("info", "night.fwr.rule_on" if r["enabled"] else "night.fwr.rule_off", rule=r["name"]))
            elif old.get("sig") != r.get("sig"):
                changes += 1
                items.append(_item("warn", "night.fwr.rule_changed", rule=r["name"]))
        for ref in sorted(set(before) - set(rules)):
            changes += 1
            items.append(_item("info", "night.fwr.rule_gone", rule=before[ref]["name"]))
        fwd_now = {f.get("name") for f in state.get("port_forwards") or []}
        fwd_before = {f.get("name") for f in prev.get("port_forwards") or []}
        for n in sorted(fwd_now - fwd_before, key=str):
            changes += 1
            items.append(_item("warn", "night.fwr.forward_new", rule=n))
        for n in sorted(fwd_before - fwd_now, key=str):
            changes += 1
            items.append(_item("info", "night.fwr.forward_gone", rule=n))
    items.sort(key=lambda i: LEVELS[i["level"]])
    look = sum(1 for i in items if i["level"] in ("alert", "warn"))
    return {"family": "openwrt", "title": "night.wrt.title", "name": name, "look": look,
            "summary": "night.wrt.summary_look" if look else "night.wrt.summary_calm",
            "params": {"n": look, "rules": len(rules), "changes": changes, "zones": len(state.get("zones") or []),
                       "ssids": len(state.get("ssids") or [])},
            "items": items[:MAX_ITEMS]}
