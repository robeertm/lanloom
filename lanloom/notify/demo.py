"""Sample data of the notification rules for the demo: a weekly maintenance window, one that ran last night, a day of
history with every outcome (sent, bundled, quiet hours, maintenance, flap, cool-down) and, in a demo instance with
accounts, rules for the first account so the rule table shows something. Nothing is ever sent from a demo."""
import json
import time


def _devices(core) -> list:
    return [d for d in core.store.devices() if not d["data"].get("is_self")]


def seed(core, now: float = None) -> None:
    n = getattr(core, "notify", None)
    if n is None or core.store.get_setting("notify_demo_seeded", False):
        return
    core.store.set_setting("notify_demo_seeded", True)
    now = now or time.time()
    devs = _devices(core)
    by_kind = {}
    for d in devs:
        by_kind.setdefault(d.get("user_kind") or d["kind"], d)
    sw = by_kind.get("switch") or (devs[0] if devs else None)
    nas = by_kind.get("nas") or (devs[1] if len(devs) > 1 else sw)
    title = lambda d: (d.get("user_title") or d["title"] or d["ip"]) if d else "Device"
    planned = [{"id": "w-demo-weekly", "kind": "weekly", "title": "Weekly backups", "days": [6], "from": "02:00", "to": "04:00",
                "devices": [nas["id"]] if nas else [], "by": "demo", "created": now - 20 * 86400},
               {"id": "w-demo-once", "kind": "once", "title": "Switch firmware", "start": now - 9 * 3600, "end": now - 8 * 3600,
                "devices": [sw["id"]] if sw else [], "by": "demo", "created": now - 26 * 3600}]
    core.store.set_setting("notify_maint", planned)
    rows = [
        (now - 21 * 3600, "dev_new_protected", "new:demo-1", "critical", {"title": "Unknown device", "ip": "192.168.50.201"}, {"*": "sent"}, None),
        (now - 15 * 3600, "dev_new", "new:demo-2", "info", {"title": "Guest phone", "ip": "192.168.50.77"}, {"*": "bundled"}, None),
        (now - 11 * 3600, "dev_offline", "off:demo-3", "info", {"title": "Tablet"}, {"*": "quiet"}, None),
        (now - 8.6 * 3600, "dev_offline", "off:%s" % (sw["id"] if sw else "sw"), "warning", {"title": title(sw), "device": sw["id"] if sw else None},
         {"*": "maintenance"}, "w-demo-once"),
        (now - 6 * 3600, "wan_down", "wan", "critical", {"name": "wan0", "resolved": now - 6 * 3600 + 70}, {"*": "flap"}, None),
        (now - 3 * 3600, "dev_offline", "off:%s" % (nas["id"] if nas else "nas"), "warning",
         {"title": title(nas), "device": nas["id"] if nas else None, "resolved": now - 2.6 * 3600}, {"*": "sent"}, None),
        (now - 2 * 3600, "dev_offline", "off:%s" % (nas["id"] if nas else "nas"), "warning",
         {"title": title(nas), "device": nas["id"] if nas else None}, {"*": "cooldown"}, None),
        (now - 1.5 * 3600, "cert_expiring", "cert:demo:controller", "warning", {"name": "Network controller", "days": 11}, {"*": "nightly"}, None),
    ]
    for t, eid, key, sev, data, outcome, maint in rows:
        data = {k: v for k, v in data.items() if v is not None}
        core.store.execute("INSERT INTO notify_log(t, event, key, severity, data, outcome, maint) VALUES(?,?,?,?,?,?,?)",
                           (t, eid, key, sev, json.dumps(data), json.dumps(outcome), maint))
    core.store.set_setting("notify_cfg", {"daily_cap": 20, "hold_rounds": 3, "cooldown_h": 12, "protected_cidrs": ["192.168.50.192/26"]})
    core.store.set_setting("notify_heartbeat", {"enabled": True, "every_min": 5, "last_t": now - 120, "ok": True,
                                                "key": "report.send.ok", "fails": 0, "last_ok": now - 120})
    if core.vault:
        core.vault.put("notify:heartbeat:url", "https://status.example/ping/demo")
    try:
        first = core.store.one("SELECT id FROM users ORDER BY id LIMIT 1")
    except Exception:
        first = None
    if first:
        n.update_prefs(first["id"], {
            "rules": {"dev_new_protected": {"mode": "immediate", "channels": ["webpush", "ntfy"], "min": "info"},
                      "wan_down": {"mode": "immediate", "channels": ["webpush"], "min": "info"},
                      "dev_offline": {"mode": "hourly", "channels": ["webpush"], "min": "warning"},
                      "dev_new": {"mode": "hourly", "channels": ["webpush"], "min": "info"}},
            "quiet": {"enabled": True, "from": "22:00", "to": "07:00"}})
