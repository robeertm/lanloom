"""Builds the map views (zones, components, traces) and the live overlay from the core state.

Texts that need translation travel as keys (`title_key`, `subtitle_key` + `vars`); the browser translates them.
"""
import ipaddress
import re
import time
from urllib.parse import urlparse

from . import hubmerge, live, topology, zones as zones_mod
from .core import FLOW_KEEP, PRESENCE_INTERVAL, flow_trace, lan_trace, topic_trace, uplink_trace

ZONE_OF_KIND = {
    "internet": "edge", "router": "edge",
    "switch": "infra", "ap": "infra",
    "self": "services", "nas": "services", "server": "services", "hub": "services", "broker": "services", "service": "services",
    "computer": "clients", "phone": "clients",
    "tv": "media", "media": "media", "camera": "media",
    "iot": "smart",
    "printer": "other", "unknown": "other",
}
ZONE_ORDER = ["edge", "infra", "services", "clients", "media", "smart", "other"]
KIND_ORDER = ["router", "switch", "ap", "self", "nas", "server", "hub", "broker", "service", "computer", "phone", "tv", "media",
              "camera", "iot", "printer", "unknown"]


def _host_of(url_or_host: str) -> str:
    if "://" in (url_or_host or ""):
        return urlparse(url_or_host).hostname or ""
    return url_or_host or ""


def _visible_devices(core) -> list:
    # a second interface of a device is drawn as part of that device (its card lists the interface)
    return [d for d in core.devices() if not d["hidden"] and not d.get("merged_into")]


def _special_components(core, devices: list) -> dict:
    """Integration-backed components: which device is the hub / broker / SNMP target (or a virtual one)."""
    by_ip = {d["ip"]: d for d in devices}
    out = {"hub": {}, "broker": {}, "snmp": {}}
    for integ in core.integrations():
        host = _host_of(integ["config"].get("url") or integ["config"].get("host") or "")
        dev = by_ip.get(host)
        kind = integ["kind"]
        if kind == "hub":
            out["hub"][integ["id"]] = dev["id"] if dev else "hub:%d" % integ["id"]
        elif kind == "mqtt":
            out["broker"][integ["id"]] = dev["id"] if dev else "broker:%d" % integ["id"]
        elif kind == "snmp":
            out["snmp"][integ["id"]] = dev["id"] if dev else "snmp:%d" % integ["id"]
    return out


def _device_component(core, dev: dict, special_kind: str = None) -> dict:
    kind = dev["kind"]
    if special_kind and kind in ("unknown", "server", "iot", "service"):
        kind = special_kind
    comp = {"id": dev["id"], "kind": kind, "title": dev["title"], "subtitle": dev["ip"] or dev.get("mac") or "", "status": dev["status"],
            "grade": "unknown", "metric": None, "badges": dev["services"][:3]}
    if not dev["title"]:
        comp["title_key"] = "kind." + kind
        comp["title_suffix"] = "." + dev["ip"].rsplit(".", 1)[-1] if "." in (dev["ip"] or "") else ""
    if dev["is_self"]:
        comp["title_key"] = "kind.self"
        comp["title"] = ""
        comp["title_suffix"] = ""
    return comp


MAX_RADIO_ON_MAP = 36           # radio devices drawn per network; the rest share one "+N more" tile


def _mesh_fact(kind: str) -> dict:
    """Physical fact of a radio lane: dashed like a wireless link, with the radio mark of its network kind."""
    return {"port": None, "own_port": None, "medium": "radio", "band": None, "ssid": None, "source": "hub", "uplink": False,
            "mesh": kind}


def _lane_fact() -> dict:
    """A radio device behind its coordinator: dashed like a wireless link; the radio mark sits on the network's own lane."""
    return {"port": None, "own_port": None, "medium": "radio", "band": None, "ssid": None, "source": "hub", "uplink": False}


def _hub_badge(comp: dict) -> None:
    comp["hub"] = True
    comp["badges"] = (["HUB"] + [b for b in comp.get("badges") or [] if b != "HUB"])[:3]


def _radio_overview(core, special: dict, comps: list, zone, add_trace) -> None:
    """Overview: one tile per radio network of each hub, on a dashed lane from the hub."""
    for integ_id, hub_comp in special["hub"].items():
        radio = core.hub_radio(integ_id)
        if not radio["networks"]:
            continue
        zid = "radios:%d" % integ_id
        zone(zid, "zone.radios", order=2.2 + integ_id / 1000)
        for net in radio["networks"]:
            cid = "rn:%d:%s" % (integ_id, net["key"])
            comps.append({"id": cid, "zone": zid, "kind": "ap", "title": "", "title_key": "radio.kind." + net["kind"],
                          "subtitle": "", "subtitle_key": "comp.radio.sub", "vars": {"devices": net["devices"] + net["anchors"], "silent": net["silent"]},
                          "status": "down" if net["health"] == "bad" else "up", "grade": "unknown", "metric": None, "badges": [],
                          "radio": net["kind"]})
            add_trace("m:%d:%s" % (integ_id, net["key"]), hub_comp, cid, "radio", False, phys=_mesh_fact(net["kind"]))


def _radio_network(core, special: dict, merge: dict, comps: list, zone, add_trace) -> None:
    """Network view: per radio network its coordinator (or the network itself) with the radio devices behind it."""
    ids = {c["id"] for c in comps}
    for integ_id, hub_comp in special["hub"].items():
        radio = core.hub_radio(integ_id)
        if not radio["networks"]:
            continue
        snap = (core.hub.get(integ_id) or {}).get("snapshot") or {}
        devs = {d["id"]: d for d in snap.get("devices") or []}
        areas = {a["id"]: a.get("name") for a in snap.get("areas") or []}
        if hub_comp not in ids:
            zone("radios:%d" % integ_id, "zone.radios", order=79 + integ_id / 1000)
            comps.append({"id": hub_comp, "zone": "radios:%d" % integ_id, "kind": "hub", "title": _integ_name(core, integ_id),
                          "subtitle": _integ_host(core, integ_id), "status": "unknown", "grade": "unknown", "metric": None,
                          "badges": []})
            ids.add(hub_comp)
        for n, net in enumerate(radio["networks"]):
            zid = "radio:%d:%s" % (integ_id, net["key"])
            zone(zid, "zone.radio." + net["kind"], order=80 + integ_id / 1000 + n / 100)
            coord = net["coordinator"]
            if coord:
                parent = merge["by_hub"].get(hubmerge.hub_key(integ_id, coord))
                if parent not in ids:
                    parent = "rd:%d:%s" % (integ_id, coord)
                    d = devs.get(coord) or {}
                    comps.append({"id": parent, "zone": zid, "kind": "ap", "title": d.get("name") or "", "title_key": "radio.role.coordinator",
                                  "subtitle": d.get("model") or "", "status": "up", "grade": "unknown", "metric": None,
                                  "badges": [], "radio": net["kind"]})
                    ids.add(parent)
                    add_trace("r:%d:%s" % (integ_id, coord), hub_comp, parent, "radio", False, phys=_mesh_fact(net["kind"]))
                    marked = True
                else:
                    marked = False             # a coordinator on the network: the mark goes on its first radio lane
            else:
                marked = True
                parent = "rn:%d:%s" % (integ_id, net["key"])
                comps.append({"id": parent, "zone": zid, "kind": "ap", "title": "", "title_key": "radio.kind." + net["kind"],
                              "subtitle": "", "subtitle_key": "comp.radio.sub", "vars": {"devices": net["devices"] + net["anchors"], "silent": net["silent"]},
                              "status": "down" if net["health"] == "bad" else "up", "grade": "unknown", "metric": None,
                              "badges": [], "radio": net["kind"]})
                ids.add(parent)
                add_trace("m:%d:%s" % (integ_id, net["key"]), hub_comp, parent, "radio", False, phys=_mesh_fact(net["kind"]))
            st = radio["state"]
            members = sorted(net["members"], key=lambda m: (devs[m].get("role") not in ("receiver", "border_router"),
                                                           (st.get(m) or {}).get("fresh") != "silent",
                                                           str(devs[m].get("name") or "").lower(), m))
            for m in members[:MAX_RADIO_ON_MAP]:
                d = devs[m]
                cid = merge["by_hub"].get(hubmerge.hub_key(integ_id, m))
                if cid not in ids:
                    cid = "rd:%d:%s" % (integ_id, m)
                    fresh = (st.get(m) or {}).get("fresh")
                    comps.append({"id": cid, "zone": zid, "kind": "ap" if d.get("role") in ("receiver", "border_router") else "iot",
                                  "title": d.get("name") or "", "title_key": "kind.iot", "subtitle": areas.get(d.get("area")) or d.get("model") or "",
                                  "status": {"silent": "down", "ok": "up"}.get(fresh, "unknown"), "grade": "unknown",
                                  "metric": None, "badges": [], "radio": net["kind"]})
                    ids.add(cid)
                add_trace("r:%d:%s" % (integ_id, m), parent, cid, "radio", False, phys=_lane_fact() if marked else _mesh_fact(net["kind"]))
                marked = True
            rest = len(members) - MAX_RADIO_ON_MAP
            if rest > 0:
                cid = "rm:%d:%s" % (integ_id, net["key"])
                comps.append({"id": cid, "zone": zid, "kind": "iot", "title": "", "title_key": "comp.radio.more",
                              "subtitle": "", "vars": {"n": rest}, "status": "unknown", "grade": "unknown", "metric": None,
                              "badges": [], "radio": net["kind"]})
                add_trace("r:%d:more:%s" % (integ_id, net["key"]), parent, cid, "radio", False, phys=_lane_fact())


def build(core, view: str = "overview") -> dict:
    devices = _visible_devices(core)
    special = _special_components(core, devices)
    merge = core.hub_merge() if core.hub else {"by_net": {}, "by_hub": {}, "suggest": {}}
    special_kind = {}
    for dev_id in special["hub"].values():
        special_kind[dev_id] = "hub"
    for dev_id in special["broker"].values():
        special_kind.setdefault(dev_id, "broker")

    comps, traces, zones = [], [], {}
    router = next((d for d in devices if d["is_gateway"]), None)
    self_dev = next((d for d in devices if d["is_self"]), None)
    host_ok = bool(core.source_status.get("host", {}).get("ok"))
    snmp_on_router = router is not None and router["id"] in special["snmp"].values()
    now = time.time()

    def zone(zid, title_key=None, title=None, span=None, order=0):
        if zid not in zones:
            zones[zid] = {"id": zid, "title": title or "", "title_key": title_key, "kind": zid.split(":")[0], "span": span, "order": order}

    def add_trace(tid, a, b, kind, measured, **extra):
        traces.append(dict({"id": tid, "a": a, "b": b, "kind": kind, "measured": bool(measured)}, **extra))

    if router:
        zone("edge", "zone.edge", span="full", order=0)
        comps.append({"id": "internet", "zone": "edge", "kind": "internet", "title": "", "title_key": "kind.internet",
                      "subtitle": "", "subtitle_key": "comp.internet.sub", "status": "unknown", "grade": "unknown",
                      "metric": None, "badges": []})
        add_trace(uplink_trace(router["id"]), "internet", router["id"], "uplink", snmp_on_router)

    if view == "network":
        nets = []
        for net in core.networks():
            try:
                nets.append(ipaddress.ip_network(net["cidr"], strict=False))
            except (KeyError, ValueError):
                continue
        for cidr in core.store.get_setting("rescan_cidrs", []) or []:
            try:
                n = ipaddress.ip_network(cidr, strict=False)
                if n not in nets:
                    nets.append(n)
            except ValueError:
                pass
        # the networks and VLANs the adapters publish: their devices get a zone of their own, titled with the name
        zinfo = {}
        try:
            states = [{"key": e["key"], "name": e["name"], "state": e["state"]} for e in core.live_configs()]
            for z in zones_mod.build([], states):
                n = ipaddress.ip_network(z["cidr"])
                zinfo[str(n)] = z
                if n not in nets and n.version == 4:
                    nets.append(n)
        except Exception:      # a broken adapter state never breaks the map
            zinfo = {}
        phys = _physical(core, devices, router)
        carried = set()            # physical links that carry measured bytes of this host (its flows and its internet traffic)
        if self_dev and host_ok:
            for other in [router["id"] if router else None] + [d["id"] for d in devices if now - core.flow_seen.get(d["id"], 0) < FLOW_KEEP]:
                if other and other != self_dev["id"]:
                    carried.update(tid for tid, _ in core.phys_path(self_dev["id"], other) or ())
        order = 1
        net_order = {}
        for dev in sorted(devices, key=lambda d: (phys["rank"].get(d["id"], 1 << 20),
                                                  KIND_ORDER.index(d["kind"]) if d["kind"] in KIND_ORDER else 99,
                                                  _ip_key(d["ip"]))):
            comp = _device_component(core, dev, special_kind.get(dev["id"]))
            if dev["id"] in merge["by_net"]:
                _hub_badge(comp)
            link = phys["links"].get(dev["id"])
            if dev["is_gateway"]:
                comp["zone"] = "edge"
            elif dev["id"] in phys["infra"]:
                zone("cabling", "zone.cabling", span="full", order=0.5)
                comp["zone"] = "cabling"
            elif link and link["medium"] == "virtual" and link["parent"] in phys["visible"]:
                host = phys["visible"][link["parent"]]
                zid = "guests:" + link["parent"]
                if zid not in zones:
                    zone(zid, "zone.guests", order=0)
                    zones[zid]["vars"] = {"host": host["title"] or host["ip"]}
                comp["zone"] = zid
            else:
                net = max((n for n in nets if _in(dev["ip"], n)), key=lambda n: n.prefixlen, default=None)
                zid = "net:" + (str(net) if net else "other")
                if zid not in zones:
                    zone(zid, None if net else "zone.other_net", str(net) if net else None, order=order)
                    info = zinfo.get(str(net)) if net else None
                    if info and (info.get("name") or info.get("vlan")):
                        zones[zid]["title_key"] = ("zone.net_named_vlan" if info.get("name") and info.get("vlan")
                                                   else "zone.net_named" if info.get("name") else "zone.net_vlan")
                        zones[zid]["vars"] = {"name": info.get("name") or "", "vlan": info.get("vlan") or "", "cidr": str(net)}
                    net_order[zid] = order
                    order += 1
                comp["zone"] = zid
            if link:
                tid = _phys_trace_id(link)
                add_trace(tid, link["parent"], dev["id"], _phys_kind(link), tid in carried, phys=_phys_fact(link, phys))
            if router and not dev["is_gateway"] and (not link or (dev["is_self"] and not core.phys_path(dev["id"], router["id"]))):
                measured = host_ok and dev["is_self"]
                add_trace(lan_trace(router["id"], dev["id"]), router["id"], dev["id"], "lan", measured)
            comps.append(comp)
        for vid, node in sorted(phys["nodes"].items(), key=lambda kv: (phys["rank"].get(kv[0], 1 << 20), kv[0])):
            zone("cabling", "zone.cabling", span="full", order=0.5)
            comp = {"id": vid, "zone": "cabling", "kind": node["kind"] if node["kind"] in KIND_ORDER else "switch",
                    "title": node.get("title") or "", "subtitle": node.get("ip") or node.get("mac") or "", "status": "unknown",
                    "grade": "unknown", "metric": None, "badges": []}
            if not comp["title"]:
                comp["title_key"] = "kind." + comp["kind"]
            comps.append(comp)
            link = phys["links"].get(vid)
            if link:
                tid = _phys_trace_id(link)
                add_trace(tid, link["parent"], vid, _phys_kind(link), tid in carried, phys=_phys_fact(link, phys))
            elif router:
                add_trace(lan_trace(router["id"], vid), router["id"], vid, "lan", False)
        _radio_network(core, special, merge, comps, zone, add_trace)
        # guests sit right after the zone of their host
        for zid, z in zones.items():
            if zid.startswith("guests:"):
                host_zone = next((c["zone"] for c in comps if c["id"] == zid[7:]), None)
                z["order"] = (zones[host_zone]["order"] if host_zone in zones else 90) + 0.4
    else:
        for dev in devices:
            comp = _device_component(core, dev, special_kind.get(dev["id"]))
            if dev["id"] in merge["by_net"]:
                _hub_badge(comp)
            zid = ZONE_OF_KIND.get(comp["kind"], "other")
            zone(zid, "zone." + zid, span="full" if zid == "edge" else None, order=ZONE_ORDER.index(zid))
            comp["zone"] = zid
            comps.append(comp)
            if router and not dev["is_gateway"] and comp["kind"] in ("switch", "ap", "self", "nas", "server", "hub", "broker"):
                add_trace(lan_trace(router["id"], dev["id"]), router["id"], dev["id"], "lan", host_ok and dev["is_self"])

        # automation hub areas
        for integ_id, hub_comp in special["hub"].items():
            entry = core.hub.get(integ_id) or {}
            snap = entry.get("snapshot")
            if hub_comp.startswith("hub:"):
                zone("services", "zone.services", order=ZONE_ORDER.index("services"))
                comps.append({"id": hub_comp, "zone": "services", "kind": "hub", "title": _integ_name(core, integ_id),
                              "subtitle": _integ_host(core, integ_id), "status": "unknown", "grade": "unknown",
                              "metric": None, "badges": []})
            if not snap:
                continue
            zid = "areas:%d" % integ_id
            zone(zid, "zone.areas", order=2.3 + integ_id / 1000)
            counts = _area_counts(snap)
            for area in sorted(snap.get("areas") or [], key=lambda a: (-counts.get(a["id"], (0, 0))[1], a.get("name") or "")):
                n_dev, n_ent = counts.get(area["id"], (0, 0))
                if not n_ent:
                    continue
                cid = "area:%d:%s" % (integ_id, area["id"])
                comps.append({"id": cid, "zone": zid, "kind": "iot", "title": area.get("name") or area["id"],
                              "subtitle": "", "subtitle_key": "comp.area.sub", "vars": {"devices": n_dev, "entities": n_ent},
                              "status": "up", "grade": "unknown", "metric": None, "badges": []})
                add_trace("h:%d:%s" % (integ_id, area["id"]), hub_comp, cid, "integration", False)

        _radio_overview(core, special, comps, zone, add_trace)

        # MQTT topic roots
        for integ_id, broker_comp in special["broker"].items():
            if broker_comp.startswith("broker:"):
                zone("services", "zone.services", order=ZONE_ORDER.index("services"))
                comps.append({"id": broker_comp, "zone": "services", "kind": "broker", "title": _integ_name(core, integ_id),
                              "subtitle": _integ_host(core, integ_id), "status": "unknown", "grade": "unknown",
                              "metric": None, "badges": ["MQTT"]})
            roots = core.topic_order.get(integ_id) or []
            if not roots:
                continue
            zid = "topics:%d" % integ_id
            zone(zid, "zone.topics", order=2.6 + integ_id / 1000)
            for root in roots:
                cid = "topic:%d:%s" % (integ_id, root)
                comps.append({"id": cid, "zone": zid, "kind": "service", "title": root, "subtitle": "",
                              "subtitle_key": "comp.topic.sub", "status": "up", "grade": "unknown", "metric": None,
                              "badges": []})
                add_trace(topic_trace(integ_id, root), broker_comp, cid, "integration", True)

        for integ_id, snmp_comp in special["snmp"].items():
            if snmp_comp.startswith("snmp:"):
                zone("infra", "zone.infra", order=ZONE_ORDER.index("infra"))
                comps.append({"id": snmp_comp, "zone": "infra", "kind": "switch", "title": _integ_name(core, integ_id),
                              "subtitle": _integ_host(core, integ_id), "status": "unknown", "grade": "unknown",
                              "metric": None, "badges": ["SNMP"]})

    # flows of this host to other devices (both views; in the network view only where no cabling carries them)
    if self_dev:
        for dev in devices:
            if view == "network" and core.phys_path(self_dev["id"], dev["id"]):
                continue
            if dev["id"] != self_dev["id"] and now - core.flow_seen.get(dev["id"], 0) < FLOW_KEEP:
                # open ports of the target from the scan: the port marks name them when no live port has been seen
                listed = sorted({p["port"] for p in dev.get("ports") or [] if isinstance(p.get("port"), int) and p.get("proto", "tcp") == "tcp"})
                add_trace(flow_trace(self_dev["id"], dev["id"]), self_dev["id"], dev["id"], "flow", True, listed=listed)

    ids = {c["id"] for c in comps}
    traces = [t for t in traces if t["a"] in ids and t["b"] in ids]
    used_zones = {c["zone"] for c in comps}
    zone_list = sorted((z for z in zones.values() if z["id"] in used_zones), key=lambda z: (z["order"], z["id"]))
    grades, metrics = live_overlay(core, now, core.last_frame.get("rates") or {}, comps)
    for c in comps:
        c["grade"] = grades.get(c["id"], c["grade"])
        if metrics.get(c["id"]):
            c["metric"] = metrics[c["id"]]
    return {"view": view, "generated": now, "structure": core.structure_version, "zones": zone_list,
            "components": comps, "traces": traces}


def _ip_key(ip: str):
    try:
        return (0, int(ipaddress.ip_address(ip)))
    except ValueError:
        return (1, 0)


def _in(ip: str, net) -> bool:
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


def _physical(core, devices: list, router) -> dict:
    """Physical links between visible components (devices and infrastructure outside the inventory), the
    infrastructure that gets its own band below the router, and a depth-first rank (parents before their children)."""
    topo = core.topology()
    visible = {d["id"]: d for d in devices}
    nodes = dict(topo["nodes"])
    known = set(visible) | set(nodes)
    links = {c: l for c, l in topo["links"].items() if c in known and l["parent"] in known}
    used = set(links) | {l["parent"] for l in links.values()}
    nodes = {k: v for k, v in nodes.items() if k in used}
    gw = router["id"] if router else None
    infra = set()
    for child, l in links.items():
        if l["medium"] != "virtual" and l["parent"] != gw and l["parent"] in visible:
            infra.add(l["parent"])
    for did, d in visible.items():
        if d["kind"] in ("switch", "ap") and did in used and did != gw:
            infra.add(did)
    infra -= set(nodes)
    children = {}
    for c, l in links.items():
        children.setdefault(l["parent"], []).append(c)
    for p in children:      # wired by port first, then radio, then guests: the zones read like a patch panel
        children[p].sort(key=lambda c: ({"radio": 1, "virtual": 2}.get(links[c]["medium"], 0), topology.port_key(links[c]["port"]), c))
    rank, n = {}, 0
    queue = ([gw] if gw else []) + sorted(p for p in children if p not in links and p != gw)
    while queue:            # breadth first: every level of the tree before the next one
        nxt = []
        for node in queue:
            if node in rank:
                continue
            rank[node] = n
            n += 1
            nxt.extend(children.get(node, []))
        queue = nxt
    names = {did: (d["title"] or "") for did, d in visible.items()}
    names.update({vid: v.get("title") or "" for vid, v in nodes.items()})
    kinds = {did: d["kind"] for did, d in visible.items()}
    kinds.update({vid: v["kind"] for vid, v in nodes.items()})
    return {"links": links, "nodes": nodes, "infra": infra, "rank": rank, "visible": visible, "names": names, "kinds": kinds,
            "infra_all": set(topo.get("infra") or [])}


def _phys_trace_id(link: dict) -> str:
    return "p:%s:%s" % (link["parent"], link["child"])


def _phys_kind(link: dict) -> str:
    return {"radio": "radio", "virtual": "virtual"}.get(link["medium"], "cable")


def _phys_fact(link: dict, phys: dict) -> dict:
    child = link["child"]
    uplink = child in phys["infra"] or child in phys["nodes"] or child in phys["infra_all"]
    return {"port": link["port"], "own_port": link["own_port"], "medium": link["medium"], "band": link["band"],
            "ssid": link["ssid"], "source": link["source"], "uplink": bool(uplink)}


def _integ_name(core, integ_id) -> str:
    row = core.store.one("SELECT name FROM integrations WHERE id=?", (integ_id,))
    return row["name"] if row else ""


def _integ_host(core, integ_id) -> str:
    row = core.store.one("SELECT config FROM integrations WHERE id=?", (integ_id,))
    if not row:
        return ""
    import json
    cfg = json.loads(row["config"])
    return _host_of(cfg.get("url") or cfg.get("host") or "")


def _area_counts(snap: dict) -> dict:
    devs, ents = {}, {}
    for d in snap.get("devices") or []:
        if d.get("area"):
            devs[d["area"]] = devs.get(d["area"], 0) + 1
    for e in snap.get("entities") or []:
        if e.get("area"):
            ents[e["area"]] = ents.get(e["area"], 0) + 1
    return {a: (devs.get(a, 0), ents.get(a, 0)) for a in set(devs) | set(ents)}


# ---------------------------------------------------------------------- live overlay
def live_overlay(core, now: float, rates: dict, comps: list = None) -> tuple:
    """component id -> live grade, component id -> short metric text."""
    grades, metrics = {}, {}
    per_comp = {}
    for trace, (r0, r1) in rates.items():
        kind, _, rest = trace.partition(":")
        if kind in ("f", "l"):
            a, _, b = rest.partition(":")
            for cid in (a, b):
                per_comp[cid] = per_comp.get(cid, 0) + r0 + r1
        elif kind == "u":
            _, _, router = rest.partition(":")
            per_comp["internet"] = per_comp.get("internet", 0) + r0 + r1
        elif kind == "q":
            integ, _, root = rest.partition(":")
            per_comp["topic:%s:%s" % (integ, root)] = r0 + r1
    unit = core.rate_unit()
    host_grade = core._source_grade("host") if "host" in core.cadence else "unknown"
    for dev_id in list(core.ip_to_device.values()):
        recent_flow = now - core.flow_seen.get(dev_id, 0) < 5
        if recent_flow:
            grades[dev_id] = host_grade
        else:
            seen = core.presence.get(dev_id)
            grades[dev_id] = live.grade_for(PRESENCE_INTERVAL, now - seen) if seen else "unknown"
        if per_comp.get(dev_id, 0) >= 1:
            metrics[dev_id] = live.human_rate(per_comp[dev_id], unit)
    router_id = core.ip_to_device.get(core.gateway_ip or "")
    for integ_id, frame in core.snmp.items():
        g = core._source_grade("int:%d" % integ_id)
        host = frame.get("host")
        dev = core.ip_to_device.get(host or "")
        target = dev or "snmp:%d" % integ_id
        grades[target] = g
        if dev and dev == router_id:
            grades["internet"] = g
    if per_comp.get("internet", 0) >= 1:
        metrics["internet"] = live.human_rate(per_comp["internet"], unit)
        if router_id:
            metrics[router_id] = metrics["internet"]
    for integ_id, entry in core.hub.items():
        g = entry["events"].grade(now) if entry.get("events") and entry["events"].last else core._source_grade("int:%d" % integ_id)
        snap = entry.get("snapshot") or {}
        for area in snap.get("areas") or []:
            grades["area:%d:%s" % (integ_id, area["id"])] = g
        grades["hub:%d" % integ_id] = g
    for integ_id, entry in core.hub.items():
        if not entry.get("snapshot"):
            continue
        radio = core.hub_radio(integ_id, now)
        for did, st in radio["state"].items():
            grades["rd:%d:%s" % (integ_id, did)] = st["grade"]
        for net in radio["networks"]:
            member = [radio["state"][m]["grade"] for m in net["members"] if m in radio["state"]]
            g = live.typical(member) if member else "unknown"
            grades["rn:%d:%s" % (integ_id, net["key"])] = "interrupted" if net["health"] == "bad" else g
            grades["rm:%d:%s" % (integ_id, net["key"])] = g
    for integ_id, entry in core.mqtt.items():
        g = core._source_grade("int:%d" % integ_id)
        grades["broker:%d" % integ_id] = g
        total = 0.0
        for root, topic in entry["topics"].items():
            cid = "topic:%d:%s" % (integ_id, root)
            grades[cid] = topic["cad"].grade(now) if topic["cad"].last else "unknown"
            total += topic["msgs"]
        metrics["broker:%d" % integ_id] = "%.0f msg/s" % total if total >= 1 else None
    return grades, metrics


# ---------------------------------------------------------------------- detail card
BUILD_TTL = 5.0


def cached_build(core, view: str) -> dict:
    """The map of a view, reused for up to 5 s while structure, map version and stored rows are unchanged. The open
    detail card asks every 5 s and needs both views; rebuilding them each time costs most at 1000 devices."""
    store = getattr(core, "_build_cache", None)
    if store is None:
        store = core._build_cache = {}
    key = (core.structure_version, getattr(core, "map_version", 0), core.store.changes())
    now = time.time()
    hit = store.get(view)
    if hit and hit[0] == key and 0 <= now - hit[1] < BUILD_TTL:
        return hit[2]
    result = build(core, view)
    store[view] = (key, now, result)
    return result


def component_traces(core, comp_id: str):
    """[(trace id, flip)] of the connections of one component (flip = the component is the second end), None if unknown."""
    view = cached_build(core, "overview")
    net_view = cached_build(core, "network")
    if not any(c["id"] == comp_id for c in view["components"] + net_view["components"]):
        return None
    out, seen = [], set()
    for t in view["traces"] + net_view["traces"]:
        if comp_id in (t["a"], t["b"]) and t["id"] not in seen:
            seen.add(t["id"])
            out.append((t["id"], t["a"] != comp_id))
    return out


def component_detail(core, comp_id: str) -> dict:
    now = time.time()
    view = cached_build(core, "overview")
    net_view = cached_build(core, "network")
    comp = next((c for c in view["components"] + net_view["components"] if c["id"] == comp_id), None)
    if not comp and comp_id.startswith("d-"):
        # a stored device that is not drawn (hidden, or a second interface of another device): its card still opens,
        # so it can be shown again, split off, or forgotten; it has no connections on the map
        stored = core.store.device(comp_id)
        if stored:
            comp = _device_component(core, core.device_view(stored))
    if not comp:
        return None
    traces = [t for t in view["traces"] + net_view["traces"] if comp_id in (t["a"], t["b"])]
    seen, series = set(), []
    for t in traces:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        s = core.ring.series(t["id"])
        if s:
            other = t["b"] if t["a"] == comp_id else t["a"]
            flip = t["a"] != comp_id
            series.append({"trace": t["id"], "kind": t["kind"], "peer": other,
                           "points": [[p[0], p[2], p[1]] if flip else list(p) for p in s]})
    out = {"component": comp, "traces": traces, "series": series, "generated": now,
           "q": core.quant.q, "ledger": {t: core.quant.ledger.get(t) for t in seen if core.quant.ledger.get(t)}}
    comps = {c["id"]: c for c in view["components"] + net_view["components"]}
    peers = {}
    for t in traces:                     # who is at the other end of each connection (the card lists them by name)
        other = comps.get(t["b"] if t["a"] == comp_id else t["a"])
        if other:
            peers[other["id"]] = {"title": other.get("title") or "", "kind": other.get("kind") or "", "device": other["id"].startswith("d-")}
    out["peers"] = peers
    dev = core.store.device(comp_id)
    if dev:
        out["device"] = core.device_view(dev)
        out["presence_age_s"] = round(now - core.presence[comp_id], 1) if comp_id in core.presence else None
    if comp_id.startswith("area:"):
        _, integ_id, area_id = comp_id.split(":", 2)
        entry = core.hub.get(int(integ_id)) or {}
        snap = entry.get("snapshot") or {}
        states = entry.get("states") or {}
        ents = []
        for e in snap.get("entities") or []:
            if e.get("area") == area_id:
                st = states.get(e["entity_id"])
                ents.append({"id": e["entity_id"], "name": e.get("name") or e["entity_id"], "state": st[0] if st else e.get("state"),
                             "unit": e.get("unit"), "changed_s": round(now - st[1], 1) if st else None, "domain": e.get("domain")})
        out["entities"] = ents[:400]
    if comp_id.startswith("topic:"):
        _, integ_id, root = comp_id.split(":", 2)
        topic = ((core.mqtt.get(int(integ_id)) or {}).get("topics") or {}).get(root)
        if topic:
            out["topic"] = {"root": root, "msgs_s": round(topic["msgs"], 2), "bytes_s": round(topic["bytes"]),
                            "cadence_s": topic["cad"].cadence(), "last_s": round(now - topic["last"], 1) if topic["last"] else None}
    for integ_id, frame in core.snmp.items():
        host_dev = core.ip_to_device.get(frame.get("host") or "")
        if comp_id in (host_dev, "snmp:%d" % integ_id):
            out["snmp"] = {"sys": frame.get("sys"), "ifaces": frame.get("ifaces"), "age_s": round(now - frame.get("t", now), 1)}
    for integ_id, entry in core.mqtt.items():
        bid = _special_components(core, _visible_devices(core))["broker"].get(integ_id)
        if comp_id == bid:
            out["broker"] = {"topics": sorted(([r, round(t["msgs"], 2), round(t["bytes"])] for r, t in entry["topics"].items()),
                                              key=lambda x: -x[2])[:30], "stats": entry.get("broker")}
    out.update(hub_detail(core, comp_id, bool(dev)))
    for integ_id, hub_comp in _special_components(core, _visible_devices(core))["hub"].items():
        if comp_id == hub_comp:               # the bridge card: what is read, what stays out, how the hub is spared
            out["hub_bridge"] = core.hub_bridge(integ_id)
    if comp_id == core.ip_to_device.get(core.self_ip or ""):
        out["ifaces"] = core.self_iface
    out.update(hypervisor_detail(core, comp_id, dev))
    out["physical"] = physical_detail(core, comp_id, bool(dev))
    return out


def hub_device_view(core, integ_id: int, did: str, match: dict | None = None, now: float | None = None) -> dict | None:
    """The hub side of a component: what the hub's registry says about the device, its entities and its freshness."""
    now = time.time() if now is None else now
    entry = core.hub.get(integ_id) or {}
    snap = entry.get("snapshot") or {}
    d = next((x for x in snap.get("devices") or [] if x.get("id") == did), None)
    if d is None:
        return None
    states = entry.get("states") or {}
    areas = {a["id"]: a.get("name") for a in snap.get("areas") or []}
    radio = core.hub_radio(integ_id, now)
    ents = []
    for e in snap.get("entities") or []:
        if e.get("device_id") != did or e.get("domain") in ("automation", "script"):
            continue
        st = states.get(e["entity_id"])
        ents.append({"id": e["entity_id"], "name": e.get("name") or e["entity_id"], "state": st[0] if st else e.get("state"),
                     "unit": e.get("unit"), "dc": e.get("dc"), "domain": e.get("domain"), "cat": e.get("cat"),
                     "changed_s": round(now - st[1], 1) if st else None})
    ents.sort(key=lambda e: (e["cat"] is not None, str(e["name"]).lower()))
    net_key = radio["dev_net"].get(did)
    net = next((n for n in radio["networks"] if n["key"] == net_key), None)
    fresh = radio["state"].get(did) or hubmerge.device_state([e for e in snap.get("entities") or [] if e.get("device_id") == did], states, now)
    return {"integ": integ_id, "integ_name": _integ_name(core, integ_id), "device_id": did, "name": d.get("name"),
            "manufacturer": d.get("manufacturer"), "model": d.get("model"), "sw": d.get("sw"), "area": areas.get(d.get("area")),
            "domains": d.get("domains") or [], "radio": d.get("radio"), "role": d.get("role"),
            "network": {"key": net["key"], "kind": net["kind"], "coordinator": net["coordinator_name"]} if net else None,
            "fresh": fresh, "match": match, "entities": ents[:80], "entities_total": len(ents)}


def radio_net_view(core, integ_id: int, key: str, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    radio = core.hub_radio(integ_id, now)
    net = next((n for n in radio["networks"] if n["key"] == key), None)
    if net is None:
        return None
    snap = (core.hub.get(integ_id) or {}).get("snapshot") or {}
    devs = {d["id"]: d for d in snap.get("devices") or []}
    areas = {a["id"]: a.get("name") for a in snap.get("areas") or []}
    members = []
    for m in net["members"]:
        st = radio["state"].get(m) or {}
        d = devs.get(m) or {}
        members.append({"device_id": m, "name": d.get("name") or m, "area": areas.get(d.get("area")), "role": d.get("role"),
                        "fresh": st.get("fresh"), "battery": st.get("battery"), "battery_low": st.get("battery_low"),
                        "last_seen_s": st.get("last_seen_s")})
    members.sort(key=lambda x: ({"silent": 0, "unknown": 1}.get(x["fresh"], 2), str(x["name"]).lower()))
    return dict({k: v for k, v in net.items() if k != "members"}, integ=integ_id, integ_name=_integ_name(core, integ_id),
                members=members[:300])


def hub_detail(core, comp_id: str, is_device: bool) -> dict:
    """Hub sections of a component card: the hub side of a merged network device (with the evidence of the match),
    a radio device, a radio network."""
    if not core.hub:
        return {}
    out = {}
    if is_device:
        merge = core.hub_merge()
        sides = [v for v in (hub_device_view(core, m["integ"], m["device_id"], m) for m in merge["by_net"].get(comp_id) or []) if v]
        if sides:
            out["hub_side"] = sides
        sugg = [v for v in (hub_device_view(core, m["integ"], m["device_id"], m) for m in merge["suggest"].get(comp_id) or []) if v]
        if sugg:
            out["hub_suggest"] = sugg
        for side in sides:
            if side.get("role") == "coordinator" and side.get("network"):
                out["radio_net"] = radio_net_view(core, side["integ"], side["network"]["key"])
                break
    elif comp_id.startswith("rd:"):
        _, integ, did = comp_id.split(":", 2)
        if integ.isdigit():
            view = hub_device_view(core, int(integ), did)
            if view:
                out["hub_device"] = view
                if view.get("role") == "coordinator" and view.get("network"):
                    out["radio_net"] = radio_net_view(core, int(integ), view["network"]["key"])
    elif comp_id.startswith(("rn:", "rm:")):
        _, integ, key = comp_id.split(":", 2)
        if integ.isdigit():
            net = radio_net_view(core, int(integ), key)
            if net:
                out["radio_net"] = net
    return out


def hypervisor_detail(core, comp_id: str, dev) -> dict:
    """Sections of a hypervisor read half on the component card: ``hypervisor`` on the card of a node (found by its
    address, or the node's own map tile), ``guest`` on the card of a guest (found by the hash of a network card's
    hardware address or by an address the guest reports). Read only; nothing here can change anything."""
    from . import integrations
    entries = [e for e in core.live_configs() if (e.get("state") or {}).get("hosts") is not None]
    if not entries:
        return {}
    ip = (dev or {}).get("ip")
    out = {}
    for e in entries:
        st = e["state"]
        try:
            hash_of = getattr(integrations.load_model(e.get("family")), "mac_hash", None)
        except (KeyError, ImportError):
            hash_of = None
        mh = hash_of((dev or {}).get("mac")) if hash_of else None
        hosts = {h["id"]: h for h in st.get("hosts") or []}
        nics = {}
        for n in st.get("guest_nics") or []:
            nics.setdefault(n["guest"], []).append(n)
        meta = {"name": e["name"], "age_s": e.get("age_s"), "version": (st.get("controller") or {}).get("version"),
                "cluster": (st.get("controller") or {}).get("cluster")}

        def facts_of(subjects):
            return [f for f in st.get("findings") or [] if f.get("subject") in subjects]

        host = next((h for h in hosts.values() if (ip and h.get("ip") == ip) or comp_id == "hv:" + re.sub(r"[^0-9a-zA-Z_.-]", "", h["name"])[:40]), None)
        if host and "hypervisor" not in out:
            guests = [dict({k: v for k, v in g.items() if k != "rrd"}, nics=nics.get(g["id"], []))
                      for g in st.get("guests") or [] if g.get("host") == host["id"]]
            guests.sort(key=lambda g: (g["template"], g["status"] != "running", g["vmid"]))
            storage = [s for s in st.get("storage") or [] if s.get("host") in (host["id"], None)]
            subjects = {host["id"]} | {g["id"] for g in guests} | {n["id"] for g in guests for n in g["nics"]} | {s["id"] for s in storage}
            out["hypervisor"] = dict(meta, host=host, guests=guests[:300], storage=storage[:64],
                                     bridges=[b for b in st.get("bridges") or [] if b.get("host") == host["id"]][:64],
                                     findings=facts_of(subjects)[:64])
        guest = None
        for g in st.get("guests") or []:
            if (mh and any(n.get("mac_hash") == mh for n in nics.get(g["id"], []))) or (ip and ip in (g.get("ips") or [])):
                guest = g
                break
        if guest and "guest" not in out:
            gn = nics.get(guest["id"], [])
            out["guest"] = dict(meta, guest=guest, host=hosts.get(guest.get("host")), nics=gn,
                                firewall=next((o for o in st.get("fw_options") or [] if o["scope"] == "guest" and o["owner"] == guest["id"]), None),
                                rules=[r for r in st.get("guest_rules") or [] if r.get("owner") == guest["id"]][:100],
                                findings=facts_of({guest["id"]} | {n["id"] for n in gn})[:32])
    return out


def physical_detail(core, comp_id: str, editable: bool) -> dict:
    """Connection section of the component card: the winning link, what other sources say, the devices on its
    ports, and (for devices) the choices of the manual "connected via" editor."""
    topo = core.topology()
    devices = {d["id"]: d for d in core.devices()}

    def ref(node_id):
        if node_id in devices:
            d = devices[node_id]
            if d["is_self"]:
                return {"id": node_id, "title": "", "kind": "self", "ip": d["ip"]}     # named "This host" like its tile
            return {"id": node_id, "title": d["title"], "kind": d["kind"], "ip": d["ip"]}
        n = topo["nodes"].get(node_id)
        if n:
            return {"id": node_id, "title": n.get("title") or "", "kind": n["kind"], "ip": n.get("ip") or ""}
        return {"id": node_id, "title": "", "kind": "unknown", "ip": ""}

    out = {}
    link = topo["links"].get(comp_id)
    if link:
        l = dict(link)
        l["parent_ref"] = ref(link["parent"])
        l["conflicts"] = [dict(c, parent_ref=ref(c["parent"])) for c in link["conflicts"]]
        l["loop"] = [dict(c, parent_ref=ref(c["parent"])) for c in link.get("loop") or []]
        if l["vars"].get("via"):
            l["vars"] = dict(l["vars"], via_ref=ref(l["vars"]["via"]))
        out["link"] = l
    kids = topo["children"].get(comp_id) or []
    if kids:
        out["children"] = [dict(ref(c), port=topo["links"][c]["port"], medium=topo["links"][c]["medium"],
                                band=topo["links"][c]["band"], source=topo["links"][c]["source"]) for c in kids][:300]
    unknown = [u for u in topo["unknown"] if u["parent"] == comp_id]
    if unknown:
        out["unknown"] = unknown[:64]
    trunks = [t for t in topo["trunks"] if t["parent"] == comp_id]
    if trunks:
        out["trunks"] = trunks[:64]
    if editable:
        below = topology.subtree(topo["children"], comp_id)
        manual = core.manual_links().get(comp_id)
        choices = [ref(i) for i in sorted(devices, key=lambda i: (devices[i]["kind"] not in ("router", "switch", "ap"),
                                                                     _ip_key(devices[i]["ip"])))
                   if i != comp_id and i not in below and not devices[i]["hidden"]]
        choices += [ref(v) for v in sorted(topo["nodes"]) if v != comp_id and v not in below]
        out["manual"] = manual
        out["choices"] = choices
    return out
