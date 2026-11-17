"""Who is this device? Manufacturer, model, firmware, origin class, and "one device, several interfaces".

Deterministic and offline. Two parts:

``fingerprint(data, registry)``
    Collects what a device says about itself (UPnP description, DNS-SD TXT records, SNMP system group,
    WS-Discovery types, NetBIOS names) and what its hardware address says (registry prefix). Returns the best
    manufacturer/model/firmware with the source of each value, the origin class of the address, a confidence and
    the reasons, plus a flat ``features`` dict that later stages (inventory and life cycle) can read unchanged.
    Manufacturer and model texts always come from the device itself or the public registry, never from this code.

``groups(devices, splits, joins)``
    Finds devices that are one physical device with several interfaces (wired + wireless, a second port of a
    router, a NAS with two cards). Evidence, strongest first: same UPnP unique device name, same SNMP system name
    and object id, same NetBIOS computer name, same DNS-SD host name, neighbouring hardware addresses of the same
    registered block together with a shared name, port or first sighting within a few minutes, and a hardware address
    a wireless repeater rewrote (first three bytes replaced by a private prefix, last three kept) together with the
    same network address or the real address inside an announced name (only ever a suggestion). Name-based
    evidence counts only while both devices are current, so a replaced device is not merged with its successor.
    Pairs at or above SURE are merged automatically; probable pairs become suggestions. A user's split always
    wins, a user's join always merges. Merging only changes how the map shows the devices: nothing is deleted.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from . import oui

SURE = 0.6
PROBABLE = 0.3
FIRST_SEEN_WINDOW_S = 10 * 60
ADJACENT_MAX = 7

# DNS-SD TXT keys a device uses for its own model / maker / firmware
TXT_MODEL = ("model", "md", "ty", "usb_mdl", "product", "am", "mdl", "modelid")
TXT_MAKER = ("manufacturer", "usb_mfg", "mfg", "vendor", "mf")
TXT_FIRMWARE = ("fw", "fwversion", "fw_version", "firmware", "swversion", "sw", "version", "ver", "vs")
_VERSION_RX = re.compile(r"(?:\bv(?:er(?:sion)?)?\.?\s*|\bfirmware\s*|\brelease\s*)(\d+(?:\.\d+){1,3}[a-z0-9-]*)", re.I)
_BARE_VERSION_RX = re.compile(r"\b(\d+\.\d+(?:\.\d+){0,2})\b")
GENERIC_NAMES = {"localhost", "workgroup", "unknown", "esp", "device", "host", "phone", "tablet", "laptop", "desktop",
                 "default", "user", "admin", "router", "gateway", "printer", "camera", "tv"}
SOURCES_RANK = {"upnp": 3, "mdns": 3, "snmp": 3, "wsd": 2, "oui": 1, "name": 1}


def _text(v, n: int = 64) -> str:
    if not isinstance(v, str):
        return ""
    return re.sub(r"\s+", " ", "".join(ch if ch.isprintable() else " " for ch in v)).strip()[:n]


def _txt_value(txt: dict, keys) -> str:
    low = {str(k).lower(): v for k, v in (txt or {}).items()}
    for k in keys:
        v = _text(low.get(k))
        if v and v.lower() not in ("unknown", "none", "n/a", "-", "0"):
            return v
    return ""


def _firmware_from(text: str) -> str:
    m = _VERSION_RX.search(text or "")
    return m.group(1)[:32] if m else ""


_HEX12_RX = re.compile(r"(?<![0-9a-f])([0-9a-f]{12})(?![0-9a-f])")


def name_mac(mac: str, data: dict) -> Optional[str]:
    """The device's real hardware address when a repeater rewrote it: many small devices put their address into
    their host or DNS-SD instance name. Counts only if that address is a registered one and keeps the last three
    bytes of the address seen on the network (a repeater only replaces the first three)."""
    if not mac:
        return None
    for hx in _HEX12_RX.findall(_names(data)):
        real = oui.norm_mac(hx)
        if real and real != mac and real[9:] == mac[9:] and not oui.is_random(real) and not oui.is_group(real):
            return real
    return None


def fingerprint(data: dict, registry: Optional[oui.Oui] = None) -> dict:
    """Identity facts for one stored device (ScanResult host + classifier fields)."""
    reg = registry or oui.REGISTRY
    mac = oui.norm_mac(data.get("mac"))
    origin = reg.origin_of(mac)
    reg_name = reg.lookup(mac) if mac else None
    cand = {"vendor": [], "model": [], "firmware": []}
    feats: dict = {}

    def add(field, value, source, why):
        value = _text(value, 64 if field != "firmware" else 32)
        if value:
            cand[field].append((SOURCES_RANK.get(source, 1), value, source, why))

    for dev in data.get("ssdp") or []:
        add("vendor", dev.get("manufacturer"), "upnp", "upnp:manufacturer")
        add("model", dev.get("model"), "upnp", "upnp:model")
        if dev.get("manufacturer"):
            feats.setdefault("upnp_manufacturer", _text(dev.get("manufacturer")))
        if dev.get("model"):
            feats.setdefault("upnp_model", _text(dev.get("model")))
        if dev.get("udn"):
            feats.setdefault("upnp_udn", _text(dev.get("udn")))
        add("firmware", _firmware_from(dev.get("server") or ""), "upnp", "upnp:server")
    for svc in data.get("mdns") or []:
        txt = svc.get("txt") or {}
        add("vendor", _txt_value(txt, TXT_MAKER), "mdns", "mdns:txt")
        model = _txt_value(txt, TXT_MODEL)
        add("model", model, "mdns", "mdns:txt")
        if model:
            feats.setdefault("mdns_model", model)
        fw = _txt_value(txt, TXT_FIRMWARE)
        if fw and re.search(r"\d", fw):
            add("firmware", fw, "mdns", "mdns:txt")
    snmp = data.get("snmp") or {}
    if snmp:
        descr = _text(snmp.get("descr"), 255)
        if descr:
            feats["sys_descr"] = descr
            add("model", descr.split(",")[0].strip()[:48], "snmp", "snmp:sysdescr")
            add("firmware", _firmware_from(descr), "snmp", "snmp:sysdescr")
        if snmp.get("object_id"):
            feats["sys_object_id"] = _text(snmp.get("object_id"), 128)
        if isinstance(snmp.get("enterprise"), int):
            feats["enterprise"] = snmp["enterprise"]
        if snmp.get("name"):
            feats["sys_name"] = _text(snmp.get("name"))
    wsd_types = sorted({_text(t, 60) for w in data.get("wsd") or [] for t in (w.get("types") or []) if _text(t)})
    if wsd_types:
        feats["wsd_types"] = wsd_types[:8]
    nb = data.get("netbios") or {}
    if nb.get("computer"):
        feats["netbios_name"] = _text(nb.get("computer"), 15)
    if nb.get("workgroup"):
        feats["workgroup"] = _text(nb.get("workgroup"), 15)
    if reg_name:
        add("vendor", reg_name, "oui", "oui:registered")
    elif mac and origin == oui.RANDOM:
        real = name_mac(mac, data)
        real_name = reg.lookup(real) if real else None
        if real_name:
            add("vendor", real_name, "name", "oui:name")
            feats["name_mac"] = real
    if mac:
        feats["oui_prefix"] = mac[:8]
    if origin == oui.RANDOM:
        feats["random_mac"] = True
    if data.get("ipv6"):
        feats["ipv6"] = len(data["ipv6"])

    out = {"origin": origin, "features": feats, "reasons": []}
    agree = 0
    for field in ("vendor", "model", "firmware"):
        items = sorted(cand[field], key=lambda c: -c[0])
        if not items:
            out[field], out[field + "_source"] = None, None
            continue
        rank, value, source, why = items[0]
        out[field], out[field + "_source"] = value, source
        out["reasons"].append(why)
        if field == "vendor":
            same = {c[2] for c in items if c[1].lower()[:6] == value.lower()[:6]}
            agree = len(same)
    # confidence of the identity (not of the device kind): self-reported values weigh more than the registry
    score = 0.0
    for field, w in (("vendor", 0.35), ("model", 0.35), ("firmware", 0.15)):
        src = out.get(field + "_source")
        if src:
            score += w * (1.0 if src not in ("oui", "name") else 0.6)
    score += 0.15 if agree > 1 else 0.0
    out["confidence"] = round(min(score, 1.0), 2)
    out["registered_name"] = reg_name
    return out


# ---------------------------------------------------------------------- one device, several interfaces
def _mdns_hosts(data: dict) -> set:
    names = set()
    for h in data.get("hostnames") or []:
        h = (h or "").lower().rstrip(".")
        if h.endswith(".local"):
            base = h[:-6]
            if base and base not in GENERIC_NAMES and len(base) >= 3:
                names.add(base)
    return names


def _names(data: dict) -> str:
    """Every name a device announced (host names and DNS-SD instance names), lower case, for address searches."""
    names = [str(h) for h in data.get("hostnames") or [] if isinstance(h, str)]
    names += [str(m.get("name")) for m in data.get("mdns") or [] if isinstance(m, dict) and isinstance(m.get("name"), str)]
    return " ".join(n.lower() for n in names)[:2000]


def _mac_hex(mac: str) -> str:
    return mac.replace(":", "") if mac else ""


def _translated(ma: str, mb: str) -> bool:
    """Two hardware addresses that differ only in the first three bytes, one of them locally administered: the
    pattern of a wireless repeater that rewrites the address of its clients (it keeps the last three bytes)."""
    return bool(ma and mb and ma != mb and ma[9:] == mb[9:] and ma[:8] != mb[:8]
                and (oui.is_random(ma) or oui.is_random(mb)))


def _keys(dev: dict) -> dict:
    data = dev.get("data") or {}
    udns = {(s.get("udn") or "").lower() for s in data.get("ssdp") or [] if s.get("udn")}
    snmp = data.get("snmp") or {}
    sys_key = None
    if snmp.get("name") and snmp.get("object_id") and _text(snmp["name"]).lower() not in GENERIC_NAMES:
        sys_key = (_text(snmp["name"]).lower(), snmp["object_id"])
    nb = ((data.get("netbios") or {}).get("computer") or "").strip().lower()
    mac = oui.norm_mac(dev.get("mac") or data.get("mac"))
    return {"udn": udns, "sys": sys_key, "nb": nb if nb and nb not in GENERIC_NAMES else "", "mdns": _mdns_hosts(data),
            "mac": mac, "first": float(dev.get("first_seen") or 0), "scan": dev.get("last_scan"),
            "port": dev.get("link_port"), "noip": not dev.get("ip"), "ip": dev.get("ip") or "", "names": _names(data)}


def _combine(a: float, b: float) -> float:
    return 1 - (1 - a) * (1 - b)


def pair_evidence(ka: dict, kb: dict) -> tuple:
    """(confidence, reasons) that two devices are one."""
    conf, why = 0.0, []
    current = ka["scan"] is not None and ka["scan"] == kb["scan"]
    if ka["udn"] & kb["udn"]:
        conf, why = _combine(conf, 0.9), why + ["same_udn"]
    if ka["sys"] and ka["sys"] == kb["sys"]:
        conf, why = _combine(conf, 0.85), why + ["same_sysname"]
    if current and ka["nb"] and ka["nb"] == kb["nb"]:
        conf, why = _combine(conf, 0.7), why + ["same_netbios"]
    if current and ka["mdns"] & kb["mdns"]:
        conf, why = _combine(conf, 0.65), why + ["same_mdns"]
    ma, mb = ka["mac"], kb["mac"]
    if ma and mb and not oui.is_random(ma) and not oui.is_random(mb) and ma[:8] == mb[:8]:
        diff = abs(oui.mac_int(ma) - oui.mac_int(mb))
        if 0 < diff <= ADJACENT_MAX:
            conf, why = _combine(conf, 0.4), why + ["adjacent_mac"]
            if ka["port"] and ka["port"] == kb["port"]:
                conf, why = _combine(conf, 0.35), why + ["same_port"]
            if ka["first"] and kb["first"] and abs(ka["first"] - kb["first"]) <= FIRST_SEEN_WINDOW_S:
                conf, why = _combine(conf, 0.2), why + ["first_seen_together"]
    # a repeater rewrote the address: only with a second fact (same address on the network, or the other hardware
    # address inside an announced name); at most 0.5, so it stays a suggestion and is never merged on its own
    if _translated(ma, mb):
        same_ip = bool(ka["ip"]) and ka["ip"] == kb["ip"]
        in_name = _mac_hex(ma) in kb["names"] or _mac_hex(mb) in ka["names"]
        if same_ip or in_name:
            conf, why = _combine(conf, 0.35), why + ["translated_mac"]
            if same_ip:
                conf, why = _combine(conf, 0.10), why + ["same_ip"]
            if in_name:
                conf, why = _combine(conf, 0.15), why + ["mac_in_name"]
    # an address-only device next to a device with an address: same registered block, same port, first seen together
    if ma and mb and ka["noip"] != kb["noip"] and not oui.is_random(ma) and not oui.is_random(mb) \
            and ma[:8] == mb[:8] and ka["port"] and ka["port"] == kb["port"] \
            and ka["first"] and kb["first"] and abs(ka["first"] - kb["first"]) <= FIRST_SEEN_WINDOW_S \
            and "adjacent_mac" not in why:
        conf, why = _combine(conf, 0.65), why + ["same_block_port_time"]
    return round(conf, 3), why


def _rank(dev: dict) -> tuple:
    data = dev.get("data") or {}
    return (bool(data.get("is_self")), bool(data.get("is_gateway")), not bool(dev.get("hidden")),
            not str(dev["id"]).startswith("d-ip-"),
            len(data.get("ports") or []), -float(dev.get("first_seen") or 0))


def _pair(a: str, b: str) -> tuple:
    return (a, b) if a <= b else (b, a)


def _link_pairs(devs: dict, keys: dict, links) -> dict:
    """{(a, b): (confidence, [reason])} from announced links. A link is {"macs": [...], "ips": [...], "why": key,
    "conf": number}: every device that owns one of the addresses is the same device as every other one."""
    by_mac, by_ip = {}, {}
    for i, d in devs.items():
        if keys[i]["mac"]:
            by_mac.setdefault(keys[i]["mac"], []).append(i)
        if d.get("ip"):
            by_ip.setdefault(d["ip"], []).append(i)
    out: dict = {}
    for link in links or []:
        ids = set()
        for m in link.get("macs") or []:
            ids.update(by_mac.get(oui.norm_mac(m), ()))
        for ip in link.get("ips") or []:
            ids.update(by_ip.get(ip, ()))
        ids = sorted(ids)
        if not 2 <= len(ids) <= 8:
            continue
        conf, why = float(link.get("conf") or 0.8), str(link.get("why") or "linked")
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                pair = _pair(ids[x], ids[y])
                old = out.get(pair)
                out[pair] = (_combine(old[0], conf), old[1] + [why]) if old and why not in old[1] \
                    else old or (conf, [why])
    return out


def groups(devices: Iterable[dict], splits: Iterable = (), joins: Iterable = (), links: Iterable = ()) -> dict:
    """{"members": {secondary: {primary, confidence, reasons, manual}}, "aliases": {primary: [secondary...]},
    "suggestions": [{a, b, confidence, reasons}]}"""
    devs = {d["id"]: d for d in devices}
    split = {_pair(*p) for p in splits if len(p) == 2}
    join = {_pair(*p) for p in joins if len(p) == 2}
    keys = {i: _keys(d) for i, d in devs.items()}
    # index candidates by shared keys instead of comparing all pairs
    buckets: dict = {}
    for i, k in keys.items():
        for u in k["udn"]:
            buckets.setdefault(("u", u), []).append(i)
        if k["sys"]:
            buckets.setdefault(("s",) + k["sys"], []).append(i)
        if k["nb"]:
            buckets.setdefault(("n", k["nb"]), []).append(i)
        for m in k["mdns"]:
            buckets.setdefault(("m", m), []).append(i)
        if k["mac"] and not oui.is_random(k["mac"]):
            v = oui.mac_int(k["mac"])
            buckets.setdefault(("a", v >> 3), []).append(i)          # neighbours: same block of 8 (next block below)
        if k["mac"]:
            buckets.setdefault(("t", oui.mac_int(k["mac"]) & 0xFFFFFF), []).append(i)   # same last three bytes (repeater)
    cand = set()
    for ids in buckets.values():
        if 1 < len(ids) <= 16:
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    if ids[x] != ids[y]:
                        cand.add(_pair(ids[x], ids[y]))
    # neighbouring blocks: an address ending in ...7 and one in ...8
    by_block: dict = {}
    for i, k in keys.items():
        if k["mac"] and not oui.is_random(k["mac"]):
            by_block.setdefault(oui.mac_int(k["mac"]) >> 3, []).append(i)
    for blk, ids in by_block.items():
        nxt = by_block.get(blk + 1, [])
        if len(ids) > 16 or len(nxt) > 16:
            continue
        for j in nxt:
            for i in ids:
                cand.add(_pair(i, j))
    # address-only devices on the same port as another device of the same registered block
    by_port: dict = {}
    for i, k in keys.items():
        if k["port"] and k["mac"] and not oui.is_random(k["mac"]):
            by_port.setdefault(k["port"], []).append(i)
    for ids in by_port.values():
        if 1 < len(ids) <= 16:
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    if keys[ids[x]]["noip"] != keys[ids[y]]["noip"] and keys[ids[x]]["mac"][:8] == keys[ids[y]]["mac"][:8]:
                        cand.add(_pair(ids[x], ids[y]))
    announced = _link_pairs(devs, keys, links)
    cand |= set(announced)
    cand |= {p for p in join if p[0] in devs and p[1] in devs}

    parent = {i: i for i in devs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    evidence, suggestions = {}, []
    for a, b in sorted(cand):
        if (a, b) in split:
            continue
        conf, why = pair_evidence(keys[a], keys[b])
        if (a, b) in announced:
            conf, why = _combine(conf, announced[(a, b)][0]), why + [w for w in announced[(a, b)][1] if w not in why]
        manual = (a, b) in join
        if manual or conf >= SURE:
            evidence[(a, b)] = (1.0 if manual else conf, ["manual"] if manual else why, manual)
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        elif conf >= PROBABLE:
            suggestions.append({"a": a, "b": b, "confidence": conf, "reasons": why})
    # a split pair must not end up in one group through a third device: drop the weakest links of that group
    comps: dict = {}
    for i in devs:
        comps.setdefault(find(i), []).append(i)
    members, aliases = {}, {}
    for root, ids in comps.items():
        if len(ids) < 2:
            continue
        sub = _respect_splits(ids, evidence, split)
        for part in sub:
            if len(part) < 2:
                continue
            primary = max(part, key=lambda i: _rank(devs[i]))
            aliases[primary] = sorted(i for i in part if i != primary)
            for i in aliases[primary]:
                conf, why, manual = _best_link(i, part, evidence)
                members[i] = {"primary": primary, "confidence": conf, "reasons": why, "manual": manual}
    grouped = set(members) | set(aliases)
    suggestions = [s for s in suggestions if not (s["a"] in grouped and s["b"] in grouped
                                                  and (members.get(s["a"], {}).get("primary", s["a"])
                                                       == members.get(s["b"], {}).get("primary", s["b"])))]
    return {"members": members, "aliases": aliases, "suggestions": suggestions[:50]}


def _best_link(i: str, part: list, evidence: dict) -> tuple:
    best = (0.0, [], False)
    for j in part:
        e = evidence.get(_pair(i, j))
        if e and e[0] > best[0]:
            best = e
    return best


def _respect_splits(ids: list, evidence: dict, split: set) -> list:
    """Rebuild the group with the strongest links first, never joining two sides of a split pair."""
    links = sorted(((e[0], p) for p, e in evidence.items() if p[0] in ids and p[1] in ids), reverse=True)
    parent = {i: i for i in ids}
    sets = {i: {i} for i in ids}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    for _conf, (a, b) in links:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        if any(_pair(x, y) in split for x in sets[ra] for y in sets[rb]):
            continue
        parent[ra] = rb
        sets[rb] |= sets.pop(ra)
    return list(sets.values())
