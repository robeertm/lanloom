"""What uses a device by its address: read only, from what Lanloom already knows. Used by the firewall editor's preview
(moving a device or releasing its fixed address breaks whatever reaches it by the old address) and shown on a
device's sheet.

``address_users(core)`` returns {address: [{"via", "by"}]} with ``via`` one of

* ``integration`` - a hub device whose configuration page points to the address (the hub integration talks to it);
  ``by`` is the device's name and the integration's domain
* ``tracker``     - a presence tracker of the hub that follows the device by its address; ``by`` is the hub's name
  (the tracker's own entity id may name a person and is never used)
* ``automation``  - an automation or script of the hub with the address written in its configuration (a ping target,
  a command's URL); ``by`` is the automation's name
* ``lanloom``     - one of Lanloom's own sources is configured with the address (a hub, controller, broker, switch);
  ``by`` is the source's name

Nothing is asked from the hub for this: it is built from the snapshot and the configuration references the hub bridge
already keeps (addresses only, never the configuration text). Never raises; fewer findings, never wrong ones.
"""
import ipaddress
import re

MAX_PER_ADDRESS = 12
_HOST = re.compile(r"//\[?([^/\]\s:]+)")


def _ip(v):
    try:
        a = ipaddress.ip_address(str(v or "").strip())
    except ValueError:
        return None
    return None if a.is_loopback or a.is_unspecified else str(a)


def _clip(text, n: int = 80) -> str:
    return " ".join(str(text or "").split())[:n]


def address_users(core) -> dict:
    out = {}

    def add(ip, via, by):
        ip = _ip(ip)
        if not ip or not by:
            return
        rows = out.setdefault(ip, [])
        item = {"via": via, "by": _clip(by)}
        if item not in rows and len(rows) < MAX_PER_ADDRESS:
            rows.append(item)

    try:
        integs = core.integrations()
        names = {i["id"]: i.get("name") or i.get("kind") or "?" for i in integs}
    except Exception:
        names, integs = {}, []
    for integ in integs:                                   # Lanloom's own sources
        try:
            cfg = integ.get("config") or {}
            text = " ".join(str(cfg.get(k) or "") for k in ("url", "host", "broker"))
            for h in set(_HOST.findall(text) + text.split()):
                add(h, "lanloom", integ.get("name") or integ.get("kind"))
        except Exception:
            continue
    for integ_id, entry in list(getattr(core, "hub", {}).items()):
        try:
            snap = entry.get("snapshot") or {}
            hub_name = names.get(integ_id) or "hub"
            for d in snap.get("devices") or []:
                if d.get("ip_hint") and not d.get("service"):
                    dom = ", ".join((d.get("domains") or [])[:2])
                    add(d["ip_hint"], "integration", "%s (%s)" % (d.get("name") or "?", dom) if dom else d.get("name"))
            for tr in snap.get("trackers") or []:
                add(tr.get("ip"), "tracker", hub_name)
            label = {e.get("entity_id"): e.get("name") for e in snap.get("entities") or []
                     if str(e.get("entity_id") or "").startswith(("automation.", "script."))}
            deps = entry.get("deps") or {}
            for group in ("automations", "scripts"):
                for eid, refs in sorted((deps.get(group) or {}).items()):
                    for ip in (refs or {}).get("ips") or []:
                        add(ip, "automation", label.get(eid) or eid)
        except Exception:
            continue
    return out
