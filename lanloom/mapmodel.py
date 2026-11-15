"""Builds the map views (zones, components, traces) and the live overlay from the core state.

Texts that need translation travel as keys (`title_key`, `subtitle_key` + `vars`); the browser translates them.
"""
import ipaddress
import time
from urllib.parse import urlparse

from . import live, topology
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
    return [d for d in core.devices() if not d["hidden"]]


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
    comp = {"id": dev["id"], "kind": kind, "title": dev["title"], "subtitle": dev["ip"], "status": dev["status"],
            "grade": "unknown", "metric": None, "badges": dev["services"][:3]}
    if not dev["title"]:
        comp["title_key"] = "kind." + kind
        comp["title_suffix"] = "." + dev["ip"].rsplit(".", 1)[-1] if "." in dev["ip"] else ""
    if dev["is_self"]:
        comp["title_key"] = "kind.self"
        comp["title"] = ""
        comp["title_suffix"] = ""
    return comp


def build(core, view: str = "overview") -> dict:
    devices = _visible_devices(core)
    special = _special_components(core, devices)
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
                net = next((n for n in nets if _in(dev["ip"], n)), None)
                zid = "net:" + (str(net) if net else "other")
                if zid not in zones:
                    zone(zid, None if net else "zone.other_net", str(net) if net else None, order=order)
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
        # guests sit right after the zone of their host
        for zid, z in zones.items():
            if zid.startswith("guests:"):
                host_zone = next((c["zone"] for c in comps if c["id"] == zid[7:]), None)
                z["order"] = (zones[host_zone]["order"] if host_zone in zones else 90) + 0.4
    else:
        for dev in devices:
            comp = _device_component(core, dev, special_kind.get(dev["id"]))
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
def component_detail(core, comp_id: str) -> dict:
    now = time.time()
    view = build(core, "overview")
    net_view = build(core, "network")
    comp = next((c for c in view["components"] + net_view["components"] if c["id"] == comp_id), None)
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
    if comp_id == core.ip_to_device.get(core.self_ip or ""):
        out["ifaces"] = core.self_iface
    out["physical"] = physical_detail(core, comp_id, bool(dev))
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
