"""First-start assistant: what the network looks like and which adapters fit.

Everything here is read-only and gentle:

* the networks come from the host's own interfaces and routes (``scan.netinfo``), the neighbour table and the
  resolver configuration - no packet is sent to find them;
* the proposal only names private ranges of at most /22 (1024 addresses); a larger interface network is narrowed to
  the smallest range around this host that still holds every known neighbour. Nothing is scanned before the user
  ticks the ranges and agrees;
* adapter suggestions come from what the gentle scan already saw (open ports, web page titles, mDNS and SSDP
  announcements). There is never a sign-in attempt, and no request is sent for a suggestion.
"""
from __future__ import annotations

import glob
import ipaddress
import os
import re
import time

from . import classify
from .scan import neighbors, netinfo, safety

MAX_PREFIX = 22                      # largest range the assistant proposes (/22 = 1024 addresses)
RESOLV_PATH = "/etc/resolv.conf"
# DHCP lease files of common network stacks (read when present; inside a container usually absent)
LEASE_GLOBS = ("/var/lib/dhcp/*.leases", "/var/lib/dhclient/*.leases", "/var/lib/NetworkManager/*.lease",
               "/run/systemd/netif/leases/*")    # identifier: lease file locations
_IPV4 = r"(\d{1,3}(?:\.\d{1,3}){3})"
_LEASE_RES = (re.compile(r"dhcp-server-identifier\s+" + _IPV4), re.compile(r"SERVER_ADDRESS=" + _IPV4))

# built-in sources (no family package) and their gentle signs; same sign format as a family manifest's ``detect``
# the hub's announced DNS-SD type is the one the classifier knows as a sure sign of a hub (no second copy of it here)
_HUB_TYPES = tuple({"mdns": t} for t, (kind, weight) in classify.MDNS_RULES.items() if kind == "hub" and weight >= 0.8)
BUILTIN_SIGNS = {
    "hub": ({"port": 8123},) + _HUB_TYPES,
    "mqtt": ({"port": 1883}, {"port": 8883}, {"mdns": "_mqtt._tcp"}),
}
# guide per kind of source: the text keys live under setup.guide.<guide>.* (steps 1..n)
GUIDE_STEPS = {"hub": 4, "mqtt": 4, "snmp": 4, "controller": 4, "hv": 4, "family": 4}

# result key of a connection test -> what the person should look at
_REJECTED = ("auth_failed", "forbidden", "key_refused", "token_refused", "subscribe_denied")
_UNREACHABLE = ("unreachable", "refused", "timeout", "closed")


def error_category(key) -> str:
    """'ok' | 'rejected' | 'certificate' | 'unreachable' | 'other' for a result key like 'source.hub.tls'."""
    tail = str(key or "").rsplit(".", 1)[-1]
    if tail == "ok":
        return "ok"
    if tail in _REJECTED:
        return "rejected"
    if tail == "tls":
        return "certificate"
    if tail in _UNREACHABLE:
        return "unreachable"
    return "other"


# ---------------------------------------------------------------------- networks
def _private(ip) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and any(a in n for n in safety.PRIVATE_V4)


def parse_resolvers(text: str) -> list:
    """Private IPv4 name servers of a resolv.conf (loopback stubs and public resolvers are left out)."""
    out = []
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) >= 2 and p[0] == "nameserver" and _private(p[1]) and p[1] not in out:
            out.append(p[1])
    return out[:4]


def parse_leases(texts) -> list:
    out = []
    for text in texts:
        for rx in _LEASE_RES:
            for ip in rx.findall(text or ""):
                if _private(ip) and ip not in out:
                    out.append(ip)
    return out[:4]


def _read(path: str, cap: int = 1 << 16) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except OSError:
        return ""


def propose_range(cidr: str, self_ip, neighbours) -> str | None:
    """The range to propose for one interface network: the network itself when it is /22 or smaller, else the
    smallest range from /24 up to /22 around this host that holds every known neighbour (None without own address)."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None
    if net.version != 4:
        return None
    if net.prefixlen >= MAX_PREFIX:
        return str(net)
    if not self_ip:
        return None
    me = ipaddress.ip_address(self_ip)
    inside = [ipaddress.ip_address(n) for n in neighbours if _in(n, net)]
    for prefix in (24, 23, 22):
        cand = ipaddress.ip_network("%s/%d" % (me, prefix), strict=False)
        if all(a in cand for a in inside):
            return str(cand)
    return str(ipaddress.ip_network("%s/%d" % (me, MAX_PREFIX), strict=False))


def _in(ip, net) -> bool:
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


def build_proposal(nets: list, routes: list, neighbours: list, demo: bool = False) -> list:
    """Pure part of ``discover``: one entry per range the assistant offers, the proposed ones first."""
    out, seen = [], set()
    for n in nets:
        rng = propose_range(n.get("cidr", ""), n.get("self_ip"), neighbours) if not demo else n.get("cidr")
        if not rng or rng in seen:
            continue
        seen.add(rng)
        ok, key = (True, "") if demo else safety.check_cidr(rng)
        rnet = ipaddress.ip_network(rng, strict=False)
        count = sum(1 for a in neighbours if _in(a, rnet))
        out.append({"cidr": rng, "iface": n.get("iface"), "self_ip": n.get("self_ip"), "gateway": n.get("gateway"),
                    "size": rnet.num_addresses, "allowed": ok, "reason": key, "neighbours": count,
                    "narrowed": rng != n.get("cidr"), "network": n.get("cidr"), "routed": False,
                    "host_mode": bool(n.get("host_mode")) or demo})
    for r in routes:              # private networks behind a router of this host: listed, never proposed
        if not r.get("flags", 0) & netinfo.RTF_GATEWAY or r.get("dest") == "0.0.0.0":
            continue
        try:
            rnet = ipaddress.ip_network("%s/%s" % (r["dest"], r["mask"]), strict=False)
        except (KeyError, ValueError):
            continue
        if rnet.prefixlen < MAX_PREFIX or str(rnet) in seen:
            continue
        ok, key = safety.check_cidr(str(rnet))
        if not ok:
            continue
        seen.add(str(rnet))
        out.append({"cidr": str(rnet), "iface": r.get("iface"), "self_ip": None, "gateway": r.get("gateway"),
                    "size": rnet.num_addresses, "allowed": True, "reason": "", "neighbours": 0, "narrowed": False,
                    "network": str(rnet), "routed": True, "host_mode": True})
    has_gw = any(e["gateway"] and e["allowed"] and not e["routed"] for e in out)
    for e in out:
        e["suggested"] = bool(e["allowed"] and not e["routed"] and (e["gateway"] or not has_gw and (e["neighbours"] or len(out) == 1)))
    out.sort(key=lambda e: (not e["suggested"], e["routed"], not e["allowed"], e["size"]))
    return out


def discover(core) -> dict:
    """Networks, gateway, name and address servers as this host sees them (no packet sent)."""
    demo = bool(core.demo)
    nets = core.networks()
    routes, arp, resolvers, dhcp = [], {}, [], []
    if not demo:
        routes = netinfo.parse_routes(_read(netinfo.ROUTE_PATH, 1 << 20)) if os.path.exists(netinfo.ROUTE_PATH) else []
        arp = neighbors.read_arp() or {}
        resolvers = parse_resolvers(_read(RESOLV_PATH))
        texts = [_read(p) for g in LEASE_GLOBS for p in sorted(glob.glob(g))[:8]]
        dhcp = parse_leases(texts)
    proposal = build_proposal(nets, routes, sorted(arp), demo=demo)
    gateway = next((e["gateway"] for e in proposal if e["gateway"] and not e["routed"]), None)
    # name servers the inventory already knows (open DNS port), after the resolver configuration
    for d in core.devices():
        if any(p.get("port") == 53 for p in d.get("ports") or []) and d["ip"] not in resolvers and _private(d["ip"]):
            resolvers.append(d["ip"])
    if demo:
        gateway = gateway or next((d["ip"] for d in core.devices() if d.get("is_gateway")), None)
        resolvers = resolvers or ([gateway] if gateway else [])
        dhcp = [gateway] if gateway else []
    host_mode = any(e["host_mode"] for e in proposal)
    return {"networks": proposal, "gateway": gateway, "dns": resolvers[:4], "dhcp": dhcp[:4],
            "neighbours": len(arp), "host_mode": host_mode or demo, "demo": demo}


# ---------------------------------------------------------------------- adapter suggestions
def _titles(dev: dict) -> list:
    out = []
    for p in dev.get("ports") or []:
        b = p.get("banner") or {}
        for k in ("title", "server"):
            if b.get(k):
                out.append((p.get("port"), str(b[k]).lower()))
    return out


def _sign_matches(sign: dict, dev: dict) -> bool:
    open_ports = {p.get("port") for p in dev.get("ports") or []}
    if "port" in sign and sign["port"] not in open_ports:
        return False
    if "title" in sign:
        titles = _titles(dev)
        if not any(sign["title"] in text and ("port" not in sign or port == sign["port"]) for port, text in titles):
            return False
    if "mdns" in sign and not any(str(m.get("type", "")).lower() == sign["mdns"] for m in dev.get("mdns") or []):
        return False
    if "ssdp" in sign and not any(sign["ssdp"] in " ".join(str(v) for v in s.values()).lower()
                                  for s in dev.get("ssdp") or [] if isinstance(s, dict)):
        return False
    return True


def _sign_reason(sign: dict) -> dict:
    if "mdns" in sign:
        return {"key": "setup.reason.mdns", "params": {}}
    if "ssdp" in sign:
        return {"key": "setup.reason.ssdp", "params": {}}
    if "title" in sign:
        return {"key": "setup.reason.title", "params": {"port": sign.get("port") or ""}}
    return {"key": "setup.reason.port", "params": {"port": sign["port"]}}


def _configured_hosts(core) -> dict:
    """kind -> set of addresses that already have an integration of that kind."""
    out: dict = {}
    for integ in core.integrations():
        cfg = integ.get("config") or {}
        text = " ".join(str(cfg.get(k) or "") for k in ("url", "host"))
        hosts = set(re.findall(_IPV4, text)) | {m for m in re.findall(r"//([^/:\s]+)", text)}
        out.setdefault(integ["kind"], set()).update(hosts)
    return out


def _guide_of(kind: str, fam) -> str:
    if fam is None:
        return kind
    return "hv" if "guests" in (fam.capabilities.get("read") or []) else "family"


def _prefill(kind: str, fam, dev: dict, port) -> dict:
    """Form values the person would type anyway (address, port); never a credential."""
    ip = dev["ip"]
    if kind == "mqtt":
        return {"host": ip, "port": 8883 if port == 8883 else 1883, "tls": port == 8883}
    if kind == "snmp":
        return {"host": ip}
    if kind == "hub":
        return {"url": "http://%s:%d" % (ip, port or 8123)}
    if fam is not None and any(f.get("k") == "url" for f in fam.form):
        # family APIs use TLS; devices on a home network usually have a self-signed certificate
        return {"url": "https://%s" % ip if port in (None, 443) else "https://%s:%d" % (ip, port), "allow_self_signed": True}
    return {}


def suggestions(core, families=None) -> list:
    """Adapters that fit devices of the inventory, best first. Built from stored scan data only."""
    from . import integrations as registry
    fams = list(families) if families is not None else registry.families(selectable_only=True)
    done = _configured_hosts(core)
    out = []
    for dev in core.devices():
        if dev.get("hidden"):           # this host itself may run a controller or a broker: it is not skipped
            continue
        candidates = [(k, None, signs) for k, signs in BUILTIN_SIGNS.items()]
        candidates += [(f.family, f, f.detect) for f in fams if f.detect]
        for kind, fam, signs in candidates:
            hits = [s for s in signs if _sign_matches(s, dev)]
            if not hits:
                continue
            port = next((s["port"] for s in hits if "port" in s), None)
            out.append(_suggestion(core, dev, kind, fam, hits, port, done))
        # SNMP at the gateway and at switches: by role, plus the answer to the optional "public" question
        role = dev.get("is_gateway") or dev.get("kind") in ("router", "switch")
        snmp_seen = "snmp" in (dev.get("sources") or [])
        if role or snmp_seen:
            reasons = []
            if dev.get("is_gateway"):
                reasons.append({"key": "setup.reason.gateway", "params": {}})
            elif role:
                reasons.append({"key": "setup.reason.kind", "params": {}})
            if snmp_seen:
                reasons.append({"key": "setup.reason.snmp", "params": {}})
            out.append({"kind": "snmp", "family": None, "guide": "snmp", "device": dev["id"], "ip": dev["ip"],
                        "title": dev.get("title") or dev["ip"], "device_kind": dev.get("kind"),
                        "certainty": "sure" if snmp_seen else "probable", "reasons": reasons,
                        "prefill": _prefill("snmp", None, dev, None),
                        "configured": dev["ip"] in done.get("snmp", set())})
    rank = {"sure": 0, "probable": 1}
    # one suggestion per kind and address; configured ones last
    uniq = {}
    for s in out:
        uniq.setdefault((s["kind"], s["ip"]), s)
    order = {"hub": 2, "mqtt": 3, "snmp": 4}
    res = sorted(uniq.values(), key=lambda s: (s["configured"], order.get(s["kind"], 1), rank.get(s["certainty"], 2),
                                               classify_ip_key(s["ip"])))
    return res[:24]


def classify_ip_key(ip: str):
    try:
        return int(ipaddress.ip_address(ip))
    except ValueError:
        return 0


def _suggestion(core, dev, kind, fam, hits, port, done) -> dict:
    certainty = "sure" if len(hits) >= 2 or any(len(s) >= 2 for s in hits) or any("mdns" in s for s in hits) else "probable"
    return {"kind": kind, "family": fam.family if fam else None, "guide": _guide_of(kind, fam), "device": dev["id"],
            "ip": dev["ip"], "title": dev.get("title") or dev["ip"],
            "device_kind": dev.get("kind"), "certainty": certainty, "reasons": [_sign_reason(s) for s in hits],
            "prefill": _prefill(kind, fam, dev, port), "configured": dev["ip"] in done.get(kind, set())}


# ---------------------------------------------------------------------- timing
def mark_started(store) -> None:
    """The clock of the first start: the first time the assistant shows the networks after the account exists."""
    if store.get_setting("setup_started", None) is None and not store.get_setting("setup_done", False):
        store.set_setting("setup_started", time.time())


def timing(store, scan_state: dict) -> dict:
    return {"started": store.get_setting("setup_started", None),
            "first_picture_s": store.get_setting("setup_first_picture_s", None),
            "scan": {k: scan_state.get(k) for k in ("state", "started", "finished", "preview", "preview_at", "found")}}
