"""Neutral configuration model, version 2: what every adapter publishes, whatever the vendor.

Version 1 (the firewall editor since 1.2.0) knew zones, allowed zone pairs, networks and the end devices that can
be moved between networks. Version 2 keeps every v1 key with its meaning (so the editor, the simulation and an
executor that has not been updated yet keep working) and adds optional lists::

    v1  zones         [{id, name, internet, discovery, builtin?}]
    v1  allow         [[from_zone, to_zone], ...]          derived from policies when an adapter has them
    v1  networks      [{id, name, zone, cidr, vlan, management, dhcp?, internet?, purpose?, own?, gateway_ip?}]
                        dhcp = {on: bool, range: [first, last] | None}
    v1  devices       [{id, name, kind, network, ip, fixed, wired, alone_on_port, private_address, protected,
                        fixed_ip?, parent?, band?, blocked?, online?, network_by_port?}]   end devices ("clients")
                        network_by_port: the device's network follows its switch port (a client setting cannot move it)
    v2  equipment     [{id, name, kind, model, firmware, mgmt_ip, uplink {device, port}}]   network equipment
    v2  ports         [{id, device, idx, name, link, speed, poe {capable, on, mode?, watts?}, profile (id or ""),
                        profile_name?, native_network, native_vlan, tagged_mode (all|none|custom), tagged[] (network
                        ids of "custom"), is_uplink, wan?, mode? (switch|mirror|aggregate), infra?, lldp?, lldp_neighbor}]
                        a step writes poe as on/off (bool); the state keeps the details
    v2  port_profiles [{id, name, native_network, native_vlan, tagged_mode, tagged[], poe?}]   named port settings
    v2  policies      [{id, name, src {zone, match?, ports?}, dst {zone, match?}, action, proto, ports, return_traffic,
                        enabled, order, states?, scheduled?, hits?, builtin?, editable?, vendor_ref?}]
                        match = {kind: network|ip|client|region|app|domain|vpn|other, items?, count?, opposite?}
    v2  ssids         [{id, name, network, bands[], security, hidden, enabled}]
    v2  hosts         [{id, name, online, ip, version, cpu, cores, mem_used, mem_total, swap_used, swap_total, load[], uptime_s}]   hypervisor nodes
    v2  guests        [{id, host, kind (vm|lxc), vmid, name, status, cpu, mem, mem_max, disk_max, protected?, ips[],
                        firewall?, backup {last_t, age_s, in_job, failed_t}, traffic?, rrd?}]
    v2  guest_nics    [{id, guest, name, bridge, vlan, trunks[], firewall, rate_mbs, link_down, mac_hash, ips[]}]
    v2  guest_rules   [{id, scope, owner, pos, direction, action, enabled, proto, sport, dport, source, dest, ...}]
    v2  fw_rules      [{...same as guest_rules, scope dc|group|node}]   fw_options [{id, scope, owner, enabled, policy_in, ...}]
    v2  bridges       [{id, host, name, kind, vlan_aware, vids, ports[], cidr, active, mgmt, vnets[]}]
    v2  sdn_zones     [{id, name, type, bridge}]   sdn_vnets [{id, name, zone, tag, vlan_aware, subnets[]}]
    v2  storage       [{id, host?, name, type, shared, active, used, total, fraction, content[]}]
    v2  backup_jobs   [{id, enabled, schedule, storage, mode, selection, guests[], excluded[], next_t}]
    v2  findings      [{id, kind, level (alert|warn|info), subject, params}]   plain checks of the adapter
    v2  dhcp_reservations [{id, client, ip, name?, network?}]   (fixed addresses handed out by the gateway)
    v2  port_forwards [{id, name, proto, wan_port, dst_ip, dst_port, enabled, own?}]
    v2  wans          [{id, name, kind (cable|cellular), role (primary|backup), enabled, up, active, rx_bps, tx_bps, latency_ms,
                        failover?, type?}]   internet lines of a gateway: ``up`` is what the gateway reports (Lanloom also measures),
                        ``active`` says which line carries traffic, ``role: backup`` is the reserve line
    v2  cellular      [{id, line, name, tech, bands (text or list), carrier, rsrp, rsrq, sinr, rssi, data_used, data_limit, period_end_t,
                        active, cell_id?, tac?, location?}]   mobile-router figures (dBm/dB, bytes); cell_id, tac and location are admin data
    v2  wan_events    [{t, kind (up|down|failover|failback), line}]   the gateway's own event stream, where it has one
    v2  ids           {enabled, blocked_24h?}   intrusion detection of the gateway, where it tells
    v2  raw           [{id, area, title, editable: false}]  vendor objects that do not map; shown, never written

Rules for every list: ``id`` is an opaque, stable text (a hash, never a hardware address); ``own: true`` marks an
object Lanloom created (only those may be deleted); ``protected`` (a reason key) and ``is_uplink`` keep an object
out of every plan; ``editable: false`` objects are shown but never written.
"""
import re

from . import steps

V2_LISTS = ("equipment", "ports", "port_profiles", "policies", "ssids", "guests", "guest_nics", "guest_rules", "bridges",
            "dhcp_reservations", "port_forwards", "raw", "hosts", "sdn_zones", "sdn_vnets", "fw_rules", "fw_options",
            "storage", "backup_jobs", "findings", "wans", "cellular")
V1_LISTS = ("zones", "networks", "devices")
ALL_LISTS = V1_LISTS + V2_LISTS
MAC_LIKE = re.compile(r"\A[0-9a-fA-F]{2}([:-]?)[0-9a-fA-F]{2}(?:\1[0-9a-fA-F]{2}){4}\Z")
MAX_ITEMS = 20000


class ModelError(ValueError):
    pass


def validate(state) -> None:
    """Shape check of a published state (v1 or v2). Raises ModelError with a short reason."""
    if not isinstance(state, dict):
        raise ModelError("state")
    if not isinstance(state.get("devices"), list) or not isinstance(state.get("networks"), list):
        raise ModelError("v1 lists")
    for key in ALL_LISTS:
        items = state.get(key)
        if items is None:
            continue
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise ModelError(key)
        seen = set()
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("id"), str) or not it["id"]:
                raise ModelError(key + " id")
            if MAC_LIKE.match(it["id"]):
                raise ModelError(key + " id is a hardware address")
            if it["id"] in seen:
                raise ModelError(key + " duplicate id")
            seen.add(it["id"])
    for it in state.get("raw") or []:
        if it.get("editable") is not False:
            raise ModelError("raw must be editable: false")
    allow = state.get("allow")
    if allow is not None and (not isinstance(allow, list) or not all(isinstance(p, list) and len(p) == 2 for p in allow)):
        raise ModelError("allow")


def objects(state: dict, area: str) -> dict:
    return {o["id"]: o for o in state.get(area) or [] if isinstance(o, dict) and "id" in o}


def find(state: dict, step: dict):
    """The object a step acts on, from a (fresh) state; None when it does not exist."""
    kind = steps.KINDS.get(step.get("kind"))
    if kind is None:
        return None
    return objects(state, kind.area).get(steps.step_object(step))


def management_zones(state: dict) -> set:
    return {n.get("zone") for n in state.get("networks") or [] if n.get("management") and n.get("zone")}


def derive_allow(state: dict, default: str = "block") -> list:
    """Zone pairs [[from, to]] that may talk, from ``policies`` (first enabled match by ``order`` wins, only policies
    without a port restriction and without a network/group narrowing, without a connection-state filter and without a schedule decide a whole
    zone pair). ``default`` is the
    vendor's verdict for pairs no policy decides. Used by adapters that publish policies to fill the v1 ``allow``."""
    zones = [z["id"] for z in state.get("zones") or []]
    pols = sorted((p for p in state.get("policies") or [] if p.get("enabled", True)),
                  key=lambda p: (p.get("order", 0), p.get("id")))
    out = []
    for a in zones:
        for b in zones:
            if a == b:
                continue
            verdict = default
            for p in pols:
                src, dst = p.get("src") or {}, p.get("dst") or {}
                if set(src) - {"zone"} or set(dst) - {"zone"} or (p.get("ports") or "") not in ("",) \
                        or p.get("proto", "any") != "any" or p.get("states") or p.get("scheduled"):
                    continue
                if src.get("zone") in (a, "*") and dst.get("zone") in (b, "*"):
                    verdict = p.get("action")
                    break
            if verdict == "allow":
                out.append([a, b])
    return out
