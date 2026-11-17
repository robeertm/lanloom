"""What the web app knows about the guests that keep the network running (for the guest lockout rules).

``app_identity(core)`` returns ({address: reason}, {hash of a hardware address: reason}) for: this host (Lanloom),
the network's gateway and name servers (as this host sees them), and the hosts of the hub, controller and broker
integrations. Hardware addresses come from the inventory, so a guest without a guest agent (no address reported by the
hypervisor) is still recognised through its network card. Read only; never raises (fewer findings, never more).
The executor applies the same rules with what it sees on its own (``lanloom.executor.local_identity``).
"""
import ipaddress
import re

KIND_REASON = {"hub": "hv.why.hub", "mqtt": "hv.why.hub", "controller": "hv.why.controller", "snmp": None}


def _reason(kind: str):
    """Why a connection's host is critical: hub/broker, controller (every device family that is no hypervisor), or not
    at all (a hypervisor's own API address is a node, not a guest; SNMP devices are not guests either)."""
    if kind in KIND_REASON:
        return KIND_REASON[kind]
    from . import integrations
    return None if kind in integrations.families_reading("guests") else "hv.why.controller"
_HOST = re.compile(r"//\[?([^/\]\s:]+)")


def _ip(v):
    try:
        return str(ipaddress.ip_address(str(v or "").strip()))
    except ValueError:
        return None


def app_identity(core) -> tuple:
    addrs, macs = {}, {}

    def add(ip, why):
        ip = _ip(ip)
        if ip and why and ip not in addrs and not ipaddress.ip_address(ip).is_loopback:
            addrs[ip] = why

    try:
        add(getattr(core, "self_ip", None), "hv.why.self")
        devices = core.devices()
        for d in devices:
            if d.get("is_self"):
                add(d.get("ip"), "hv.why.self")
        for d in devices:
            if d.get("is_gateway"):
                add(d.get("ip"), "hv.why.gateway")
        try:
            from . import setup_assist
            info = setup_assist.discover(core)
            add(info.get("gateway"), "hv.why.gateway")
            for ip in info.get("dns") or []:
                add(ip, "hv.why.dns")
        except Exception:
            pass
        for integ in core.integrations():
            why = _reason(integ.get("kind"))
            cfg = integ.get("config") or {}
            text = " ".join(str(cfg.get(k) or "") for k in ("url", "host", "broker"))
            for h in _HOST.findall(text) + text.split():
                add(h, why)
        from . import integrations
        for d in devices:
            if d.get("ip") in addrs and d.get("mac"):
                for h in integrations.guest_mac_hashes(d["mac"]):
                    macs.setdefault(h, addrs[d["ip"]])
    except Exception:
        pass
    return addrs, macs
