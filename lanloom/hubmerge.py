"""Hub devices and network devices as one component; radio networks of the hub as lanes (core side, read only).

Matching a hub device with a network device (``match``), strongest evidence first:

* ``mac`` - a hardware address of the hub's device registry (``connections``) equals the network device's address
  (or one of its other interfaces). Confidence 0.95.
* ``tracker`` - the device's configuration page names an IP address, and a tracker of the hub knows the hardware
  address behind it, which a network device has. Confidence 0.85.
* ``ip`` - the configuration page names the network device's IP address. Confidence 0.7.
* ``name`` - the hub's device name equals a network device's name or host name, and both are unique. Confidence
  0.45: only a suggestion, never merged on its own.

From 0.6 on the two are drawn as one component; the user can take a match apart or confirm a suggestion
(``rules``: ``split``/``join`` pairs of network device id and ``<connection>:<hub device>``). The card says which
evidence decided (the source of the match).

Radio networks (``radio_view``): every hub device the registry places in a radio network (see
``sources/hub_radio.py``) belongs to one lane per network - the coordinator (stick, bridge, controller) or, for
networks without one, the network itself, with the radio devices behind it (``via``). Freshness of a radio device
follows one rule: it is fresh while at least one of its entities is not ``unavailable``, and, when the integration
reports a "last seen" time, while that time is younger than ``SILENT_S``. The age of the last state update is never
used (an update without a change, or a restart of the hub, would make every device look fresh).

"On site" (``onsite``): what needs a visit - batteries (now: at most ``BATTERY_NOW`` % or a low-battery sensor; soon:
at most ``BATTERY_SOON`` %) and silent radio devices.
"""
from __future__ import annotations

import datetime
import re
import time

from . import classify
from .sources.hub_radio import KINDS as RADIO_KINDS

AUTO = 0.6
CONF = {"mac": 0.95, "tracker": 0.85, "ip": 0.7, "name": 0.45, "manual": 1.0}
SILENT_S = 24 * 3600              # "last seen" older than this: the device is silent
BATTERY_NOW = 10
BATTERY_SOON = 25
MAX_ONSITE = 2000
UNAVAILABLE = "unavailable"
SEVERITY = {"now": 0, "silent": 1, "soon": 2, "ok": 3, "unknown": 4}
LIVE_MIN_S = 120.0                # a source without news for longer than max(this, 4 x its usual rhythm) is not fresh


def hub_key(integ_id: int, device_id: str) -> str:
    return "%d:%s" % (int(integ_id), device_id)


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _label(host) -> str:
    return _norm(str(host or "").split(".", 1)[0])


# ---------------------------------------------------------------------- matching
def match(net_devices: list, hubs: dict, rules: dict | None = None) -> dict:
    """``net_devices``: device views of the core; ``hubs``: {integration id: snapshot}. Returns
    ``{"by_net": {net id: [match, ...]}, "by_hub": {hub key: net id}, "suggest": {net id: [match, ...]}}``;
    a match is ``{"integ", "device_id", "how", "value", "confidence", "manual"}``."""
    rules = rules or {}
    split = {(a, b) for a, b in (rules.get("split") or []) if isinstance(a, str) and isinstance(b, str)}
    join = {(a, b) for a, b in (rules.get("join") or []) if isinstance(a, str) and isinstance(b, str)}
    primary = {}
    by_mac, by_ip, by_name = {}, {}, {}
    names_seen = {}
    for d in net_devices:
        pid = (d.get("merged_into") or {}).get("id") or d["id"]
        primary[d["id"]] = pid
        if d.get("mac"):
            by_mac.setdefault(d["mac"].lower(), pid)
        for a in d.get("aliases") or []:
            if a.get("mac"):
                by_mac.setdefault(a["mac"].lower(), pid)
        if d.get("ip"):
            by_ip.setdefault(d["ip"], pid)
        if d.get("merged_into") or d.get("hidden"):
            continue
        for n in {_norm(d.get("title"))} | {_label(h) for h in d.get("hostnames") or []}:
            if len(n) >= 4:
                names_seen.setdefault(n, set()).add(pid)
    for n, ids in names_seen.items():
        if len(ids) == 1:
            by_name[n] = next(iter(ids))
    known = set(primary.values())

    found = {}                                     # hub key -> match (net id inside)
    for integ_id, snap in sorted((hubs or {}).items()):
        snap = snap or {}
        tracker_mac = {t["ip"]: t["mac"] for t in snap.get("trackers") or [] if t.get("ip") and t.get("mac")}
        hub_names = {}
        for d in snap.get("devices") or []:
            if not d.get("service"):
                n = _norm(d.get("name"))
                hub_names[n] = hub_names.get(n, 0) + 1
        for d in snap.get("devices") or []:
            if d.get("service"):
                continue
            key = hub_key(integ_id, d["id"])
            best = None
            for mac in d.get("macs") or []:
                if mac.lower() in by_mac:
                    best = (by_mac[mac.lower()], "mac", mac)
                    break
            ip = d.get("ip_hint")
            if best is None and ip and tracker_mac.get(ip, "").lower() in by_mac:
                best = (by_mac[tracker_mac[ip].lower()], "tracker", ip)
            if best is None and ip and ip in by_ip:
                best = (by_ip[ip], "ip", ip)
            if best is None and (not d.get("radio") or d.get("role") in ("coordinator", "receiver", "border_router")):
                for n in (_label(d.get("host_hint")), _norm(d.get("name"))):
                    if len(n) >= 4 and n in by_name and hub_names.get(_norm(d.get("name")), 0) <= 1:
                        best = (by_name[n], "name", d.get("name") or d.get("host_hint"))
                        break
            if best is not None:
                found[key] = {"integ": integ_id, "device_id": d["id"], "net": best[0], "how": best[1],
                              "value": best[2], "confidence": CONF[best[1]], "manual": False}
    for net_id, key in join:
        net_id = primary.get(net_id, net_id)
        integ, _, did = key.partition(":")
        if net_id in known and integ.isdigit() and did:
            found[key] = {"integ": int(integ), "device_id": did, "net": net_id, "how": "manual", "value": None,
                          "confidence": CONF["manual"], "manual": True}
    by_net, suggest, by_hub = {}, {}, {}
    for key, m in found.items():
        if (m["net"], key) in split or (primary.get(m["net"]), key) in split:
            continue
        net_id = m.pop("net")
        target = by_net if m["confidence"] >= AUTO else suggest
        target.setdefault(net_id, []).append(m)
        if m["confidence"] >= AUTO:
            by_hub[key] = net_id
    for lst in list(by_net.values()) + list(suggest.values()):
        lst.sort(key=lambda m: (-m["confidence"], m["device_id"]))
    return {"by_net": by_net, "by_hub": by_hub, "suggest": suggest}


# ---------------------------------------------------------------------- class of a matched hub device
def kind_of(hub_device: dict | None) -> str | None:
    """The device class a hub device stands for, for a network device that nothing else could classify: the words
    of its model and name through the classifier, else a smart-home device (a hub registers devices it integrates).
    Services and anything without a model or name give None."""
    d = hub_device or {}
    if d.get("service"):
        return None
    words = [w for w in (d.get("model"), d.get("name")) if isinstance(w, str) and w]
    if not words and not d.get("radio"):
        return None
    res = classify.classify({"hostnames": words})
    if res["kind"] not in ("unknown", "self", "internet") and res["confidence"] >= classify.UNSURE:
        return res["kind"]
    return "iot"


# ---------------------------------------------------------------------- freshness of the source (the hub connection)
def source_live(ok, last, cadence_s, now: float) -> bool:
    """True when the hub connection delivers: no failure recorded and the last event is younger than
    max(120 s, 4 x the usual gap). Without any information (no event ever booked) there is nothing to doubt."""
    if ok is False and (not last or now - last > 30):
        return False
    if not last:
        return True
    return now - last <= max(LIVE_MIN_S, 4 * (cadence_s or 0))


# ---------------------------------------------------------------------- freshness of one hub device
def _iso_age(value, now: float):
    if not isinstance(value, str) or not value or value in ("unknown", UNAVAILABLE) or len(value) > 40:
        return None
    try:
        t = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, now - t.timestamp())


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def device_state(entities: list, states: dict, now: float, live: bool = True) -> dict:
    """Freshness, battery and availability of one hub device from its entities (snapshot + live state changes).
    ``live`` False (the hub connection is not fresh): nothing is known any more - ``fresh`` is "unknown" with ``stale`` set,
    no battery verdict, no live value."""
    if not live:
        return {"fresh": "unknown", "stale": True, "available": None, "last_seen_s": None, "battery": None,
                "battery_low": False, "grade": "unknown"}
    available, any_entity, age, battery, low = False, False, None, None, False
    for e in entities:
        st = states.get(e["entity_id"])
        state = st[0] if st else e.get("state")
        any_entity = True
        if state != UNAVAILABLE:
            available = True
        if e.get("ls"):
            a = _iso_age(state, now)
            if a is not None:
                age = a if age is None else min(age, a)
        if e.get("dc") == "battery":
            if e.get("domain") == "binary_sensor":
                low = low or state == "on"
            elif e.get("domain") == "sensor":
                v = _num(state)
                if v is not None and 0 <= v <= 100:
                    battery = v if battery is None else min(battery, v)
    if not any_entity:
        fresh = "unknown"
    elif not available or (age is not None and age > SILENT_S):
        fresh = "silent"
    else:
        fresh = "ok"
    return {"fresh": fresh, "stale": False, "available": available if any_entity else None, "last_seen_s": None if age is None else round(age),
            "battery": None if battery is None else round(battery), "battery_low": bool(low), "grade": _grade(fresh, age)}


def _grade(fresh: str, age) -> str:
    if fresh == "silent":
        return "interrupted"
    if fresh == "unknown":
        return "unknown"
    if age is None:
        return "periodic"
    return "near" if age < 3600 else ("periodic" if age < 6 * 3600 else "sluggish")


def _onsite_cat(st: dict, radio) -> tuple:
    reasons = []
    if st.get("stale"):
        return "unknown", ["stale"]
    b = st["battery"]
    if st["battery_low"]:
        reasons.append("low")
    if b is not None and b <= BATTERY_SOON:
        reasons.append("battery")
    if radio and st["fresh"] == "silent":
        reasons.append("silent")
    if st["battery_low"] or (b is not None and b <= BATTERY_NOW) or ("silent" in reasons and b is not None and b <= BATTERY_SOON):
        return "now", reasons
    if "silent" in reasons:
        return "silent", reasons
    if b is not None and b <= BATTERY_SOON:
        return "soon", reasons
    return "ok", reasons


# ---------------------------------------------------------------------- radio networks
def radio_view(snap: dict, states: dict, now: float | None = None, live: bool = True) -> dict:
    """Radio networks of one hub: ``{"networks": [...], "dev_net": {device: network key}, "state": {device: state}}``."""
    now = time.time() if now is None else now
    snap = snap or {}
    devs = {d["id"]: d for d in snap.get("devices") or [] if not d.get("service")}
    ents = {}
    for e in snap.get("entities") or []:
        if e.get("device_id") in devs:
            ents.setdefault(e["device_id"], []).append(e)
    areas = {a["id"]: a.get("name") for a in snap.get("areas") or []}
    state = {did: device_state(ents.get(did, []), states or {}, now, live) for did, d in devs.items() if d.get("radio")}
    children = {}
    for did, d in devs.items():
        if d.get("radio") and d.get("via") in devs:
            children.setdefault(d["via"], []).append(did)

    def root_of(did):
        seen, cur = {did}, did
        while True:
            via = devs[cur].get("via")
            if via not in devs or via in seen or devs[via].get("radio") != devs[did].get("radio"):
                return cur
            seen.add(via)
            cur = via

    coords = {}
    for did, d in devs.items():
        if d.get("radio") in ("mesh154", "subghz") and d.get("role") == "coordinator":
            coords.setdefault(d["radio"], []).append(did)
    nets, dev_net = {}, {}
    for did in sorted(devs, key=lambda i: (devs[i].get("name") or "", i)):
        d = devs[did]
        kind = d.get("radio")
        if kind not in RADIO_KINDS:
            continue
        if kind in ("mesh154", "subghz"):
            root = root_of(did)
            if devs[root].get("role") != "coordinator" and not children.get(root):
                # a device without a path to a coordinator: the network's only coordinator, if there is exactly one
                root = coords[kind][0] if len(coords.get(kind) or []) == 1 else None
            key = "%s:%s" % (kind, root or "_")
            coord = root
        else:
            key, coord = "%s:_" % kind, None
        net = nets.setdefault(key, {"key": key, "kind": kind, "coordinator": coord, "members": []})
        if did != coord:
            net["members"].append(did)
        dev_net[did] = key
    out = []
    for key, net in sorted(nets.items(), key=lambda kv: (RADIO_KINDS.index(kv[1]["kind"]), kv[0])):
        members = net["members"]
        anchors = [m for m in members if devs[m].get("role") in ("receiver", "border_router")]
        silent = [m for m in members if state[m]["fresh"] == "silent" and m not in anchors]
        plain = [m for m in members if m not in anchors]
        low = [m for m in plain if _onsite_cat(state[m], True)[0] in ("now", "soon") and (state[m]["battery"] is not None or state[m]["battery_low"])]
        coord = net["coordinator"]
        coord_state = state.get(coord) if coord else None
        anchor_silent = [m for m in anchors if state[m]["fresh"] == "silent"]
        if not live:
            health = "unknown"
        elif (coord_state and coord_state["fresh"] == "silent") or (plain and len(silent) * 2 >= len(plain)) or (anchors and len(anchor_silent) == len(anchors)):
            health = "bad"
        elif silent or anchor_silent:
            health = "warn"
        else:
            health = "ok"
        out.append({"key": key, "kind": net["kind"], "coordinator": coord,
                    "coordinator_name": (devs[coord].get("name") if coord else None),
                    "coordinator_fresh": coord_state["fresh"] if coord_state else None,
                    "devices": len(plain), "silent": len(silent), "battery": len(low), "anchors": len(anchors),
                    "anchors_silent": len(anchor_silent), "health": health,
                    "members": members, "area_names": areas})
    for n in out:
        n.pop("area_names", None)
    return {"networks": out, "dev_net": dev_net, "state": state, "live": live}


def onsite(snap: dict, states: dict, radio: dict, now: float | None = None) -> dict:
    """Batteries and radio: what needs a visit (now / soon / silent) and what is fine."""
    now = time.time() if now is None else now
    snap = snap or {}
    devs = {d["id"]: d for d in snap.get("devices") or [] if not d.get("service")}
    areas = {a["id"]: a.get("name") for a in snap.get("areas") or []}
    ents = {}
    for e in snap.get("entities") or []:
        if e.get("device_id") in devs:
            ents.setdefault(e["device_id"], []).append(e)
    live = (radio or {}).get("live", True)
    items, counts = [], {"now": 0, "soon": 0, "silent": 0, "ok": 0, "unknown": 0}
    for did, d in devs.items():
        st = (radio or {}).get("state", {}).get(did) or device_state(ents.get(did, []), states or {}, now, live)
        has_battery = st["battery"] is not None or any(e.get("dc") == "battery" for e in ents.get(did, []))
        if not d.get("radio") and not has_battery:
            continue
        if d.get("role") in ("receiver", "border_router", "coordinator") and not has_battery:
            continue
        cat, reasons = _onsite_cat(st, d.get("radio"))
        counts[cat] += 1
        items.append({"device_id": did, "name": d.get("name") or did, "area": areas.get(d.get("area")), "radio": d.get("radio"),
                      "battery": st["battery"], "battery_low": st["battery_low"], "last_seen_s": st["last_seen_s"],
                      "available": st["available"], "fresh": st["fresh"], "cat": cat, "reasons": reasons})
    items.sort(key=lambda i: (SEVERITY[i["cat"]], i["battery"] if i["battery"] is not None else 101, (i["name"] or "").lower()))
    return {"counts": counts, "live": live, "items": items[:MAX_ONSITE], "thresholds": {"now": BATTERY_NOW, "soon": BATTERY_SOON, "silent_h": SILENT_S // 3600}}


def health(snap: dict, radio: dict) -> dict:
    """Radio receivers (BLE adapters and proxies), Thread border routers and devices of the IP standard."""
    snap = snap or {}
    devs = {d["id"]: d for d in snap.get("devices") or [] if not d.get("service")}
    st = (radio or {}).get("state") or {}

    def row(did):
        d = devs[did]
        s = st.get(did) or {}
        return {"device_id": did, "name": d.get("name") or did, "fresh": s.get("fresh", "unknown"), "sw": d.get("sw"),
                "model": d.get("model")}
    receivers = [row(i) for i, d in sorted(devs.items(), key=lambda kv: kv[1].get("name") or "") if d.get("radio") == "ble" and d.get("role") == "receiver"]
    routers = [row(i) for i, d in sorted(devs.items(), key=lambda kv: kv[1].get("name") or "") if d.get("radio") == "thread" and d.get("role") == "border_router"]
    ipstd = [i for i, d in devs.items() if d.get("radio") == "ipstd"]
    ip_silent = [i for i in ipstd if (st.get(i) or {}).get("fresh") == "silent"]
    ble = [i for i, d in devs.items() if d.get("radio") == "ble" and d.get("role") != "receiver"]
    return {"receivers": receivers, "ble_devices": len(ble), "border_routers": routers, "live": (radio or {}).get("live", True),
            "ipstd": {"devices": len(ipstd), "silent": len(ip_silent)}}
