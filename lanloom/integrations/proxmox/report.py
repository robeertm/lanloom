"""Night report contribution of the Proxmox VE family (pure, no I/O, no clock of its own).

``night_section(previous, state, now, name)`` compares the neutral state at the last report with the state now and
lists what deserves a look in the morning, in plain words (every text is an i18n key with parameters; no internal
ids). Registered in the family manifest (``report``); the night report shows the section only when the family's
read half delivered a state.

Items, most important first:
* ``night.hv.node_offline``      a node is offline now;
* ``night.hv.guest_stopped``     a guest that was running at the last report is stopped now (templates skipped);
* ``night.hv.guest_new`` / ``night.hv.guest_gone``  guests added or removed since the last report;
* ``night.hv.backup_failed``     a backup task failed during the night;
* the adapter's findings (``hv.finding.<kind>``: storage almost full, guests without or with an old backup, bridge
  missing, VLAN not allowed on the bridge, VLAN not carried by the switch port, firewall without effect);
* ``night.hv.backup_ok``         how many guests were backed up during the night (only when nothing else failed).

Product names are used only to identify compatible products; no affiliation or endorsement.
"""

LEVELS = {"alert": 0, "warn": 1, "info": 2}
MAX_ITEMS = 40


def _item(level: str, key: str, **params) -> dict:
    return {"level": level, "key": key, "params": {k: v for k, v in params.items() if v is not None}}


def night_section(previous, state, now: float, name: str = None):
    """The section, or None when there is no state (no source, no section)."""
    if not isinstance(state, dict) or state.get("hosts") is None:
        return None
    prev = previous if isinstance(previous, dict) and previous.get("hosts") is not None else None
    items = []
    for h in state.get("hosts") or []:
        if not h.get("online"):
            items.append(_item("alert", "night.hv.node_offline", host=h.get("name")))
    guests = {g["id"]: g for g in state.get("guests") or [] if not g.get("template")}
    since = None
    if prev is not None:
        before = {g["id"]: g for g in prev.get("guests") or [] if not g.get("template")}
        for gid, g in sorted(guests.items(), key=lambda kv: kv[1].get("vmid") or 0):
            old = before.get(gid)
            if old and old.get("status") == "running" and g.get("status") == "stopped":
                items.append(_item("warn", "night.hv.guest_stopped", guest=g.get("name"), vmid=g.get("vmid")))
            elif old is None:
                items.append(_item("info", "night.hv.guest_new", guest=g.get("name"), vmid=g.get("vmid")))
        for gid, g in sorted(before.items(), key=lambda kv: kv[1].get("vmid") or 0):
            if gid not in guests:
                items.append(_item("info", "night.hv.guest_gone", guest=g.get("name"), vmid=g.get("vmid")))
        since = prev.get("t")
    window = float(now) - (float(since) if isinstance(since, (int, float)) else 86400.0)
    failed = 0
    ok = 0
    for g in guests.values():
        b = g.get("backup") or {}
        if b.get("failed_t") and b["failed_t"] >= window:
            failed += 1
            items.append(_item("warn", "night.hv.backup_failed", guest=g.get("name"), vmid=g.get("vmid")))
        elif b.get("last_t") and b["last_t"] >= window:
            ok += 1
    for f in state.get("findings") or []:
        if f.get("kind") == "backup_failed" or f.get("kind") == "node_offline":
            continue                       # already listed above in night words
        items.append(_item(f.get("level") if f.get("level") in LEVELS else "info", "hv.finding.%s" % f.get("kind"), **(f.get("params") or {})))
    if ok and not failed:
        items.append(_item("info", "night.hv.backup_ok", n=ok))
    items.sort(key=lambda i: LEVELS[i["level"]])
    look = sum(1 for i in items if i["level"] in ("alert", "warn"))
    return {"family": "proxmox", "title": "night.hv.title", "name": name, "look": look,
            "summary": "night.hv.summary_look" if look else "night.hv.summary_calm",
            "params": {"n": look, "running": sum(1 for g in guests.values() if g.get("status") == "running"), "total": len(guests),
                       "nodes": len(state.get("hosts") or [])},
            "items": items[:MAX_ITEMS]}
