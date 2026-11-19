"""Catalog of device classes and what can be connected to each (plan: "Einbinden"), and the suggestions built from it.

``catalog.json`` lists the classes. Three sources of a class:

``builtin``  - a kind of source Lanloom has always had (hub, message broker, SNMP); its gentle signs live in
               ``setup_assist.BUILTIN_SIGNS``;
``family``   - a device-family adapter of the registry; its signs are the manifest's ``detect`` list and whether it can
               also configure (an executor half) comes from its capabilities;
``catalog``  - a class with its own signs in the JSON: it is either ``available`` through one of the kinds above (a
               storage device or a mobile router is read over SNMP, a UPS over the UPS-monitoring protocol) or
               ``planned`` (a printer, a camera, a container host): shown with a read-only account guide and
               the topic it belongs to, without a button that would do nothing.

Everything is derived from stored scan data - nothing is sent to a device to build a suggestion.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import setup_assist

PATH = Path(__file__).with_name("catalog.json")
SOURCES = ("builtin", "family", "catalog")
STATUS = ("available", "planned")
MODES = ("read", "read_write", "auto")
TOPICS = ("power", "printing", "cameras", "host-agent", "dns-filter")
KEYS = {"id", "source", "kind", "guide", "mode", "benefit", "status", "steps", "topic", "detect", "device_kinds"}
SIGN_KEYS = {"port", "title", "mdns", "ssdp"}


class CatalogError(ValueError):
    pass


def _num(value, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _validate(data) -> list:
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("classes"), list):
        raise CatalogError("catalog shape")
    seen = set()
    for c in data["classes"]:
        if not isinstance(c, dict) or set(c) - KEYS or not isinstance(c.get("id"), str) or not c["id"].isidentifier():
            raise CatalogError("class %r" % (c.get("id") if isinstance(c, dict) else c))
        if c["id"] in seen:
            raise CatalogError("duplicate " + c["id"])
        seen.add(c["id"])
        if c.get("source") not in SOURCES or c.get("status") not in STATUS or c.get("mode") not in MODES:
            raise CatalogError("class %s: source/status/mode" % c["id"])
        if not _num(c.get("benefit"), 0, 100) or not _num(c.get("steps"), 1, 8):
            raise CatalogError("class %s: benefit/steps" % c["id"])
        if c["status"] == "planned" and c.get("topic") not in TOPICS:
            raise CatalogError("class %s: planned needs a known topic" % c["id"])
        if c["source"] == "catalog":
            for sign in c.get("detect") or []:
                if not isinstance(sign, dict) or not sign or set(sign) - SIGN_KEYS:
                    raise CatalogError("class %s: sign" % c["id"])
            if not c.get("detect"):
                raise CatalogError("class %s: no signs" % c["id"])
        if c["status"] == "available" and c["source"] == "catalog" and c.get("kind") not in setup_assist.GUIDE_STEPS:
            raise CatalogError("class %s: available class names a kind of source" % c["id"])
    return data["classes"]


_CACHE: list = []


def classes() -> list:
    if not _CACHE:
        _CACHE.extend(_validate(json.loads(PATH.read_text(encoding="utf-8"))))
    return [dict(c) for c in _CACHE]


def public() -> list:
    """The classes for the web page (no signs: the server decides what matches)."""
    return [{"id": c["id"], "status": c["status"], "topic": c.get("topic"), "mode": c["mode"], "benefit": c["benefit"],
             "steps": c["steps"], "guide": _guide_prefix(c)} for c in classes()]


def _guide_prefix(c: dict) -> str:
    if c["source"] == "catalog" and c["status"] == "planned":
        return "catalog.%s.guide" % c["id"]
    return "setup.guide.%s" % (c.get("guide") or c.get("kind") or c["id"])


def _family_mode(fam) -> str:
    return "read_write" if fam is not None and (fam.write or (fam.capabilities or {}).get("write")) else "read"


def _decorate(s: dict, cls: dict, fam) -> dict:
    mode = _family_mode(fam) if cls["mode"] == "auto" else cls["mode"]
    out = dict(s)
    out.update({"entry": cls["id"], "status": "available", "mode": mode, "benefit": cls["benefit"], "topic": None,
                "guide_prefix": _guide_prefix(cls), "steps": cls["steps"]})
    return out


def _class_for(s: dict, by_id: dict):
    if s["family"]:
        from .. import integrations as registry
        own = registry.get(s["family"]).catalog           # a family may belong to a class of its own (a DNS filter)
        if own and own in by_id:
            return by_id[own]
        return by_id["hypervisor" if s["guide"] == "hv" else "controller"]
    return by_id.get(s["kind"])


def _planned_or_extra(core, cls: dict, done: dict) -> list:
    out = []
    for dev in core.devices():
        if dev.get("hidden") or dev.get("merged_into"):
            continue
        hits = [sg for sg in cls["detect"] if setup_assist._sign_matches(sg, dev)]
        kind_hit = dev.get("kind") in (cls.get("device_kinds") or [])
        if not hits and not kind_hit:
            continue
        reasons = [setup_assist._sign_reason(sg) for sg in hits]
        if kind_hit:
            reasons.append({"key": "setup.reason.class", "params": {}})
        sure = kind_hit and bool(hits) or len(hits) >= 2
        kind = cls.get("kind")
        item = {"kind": kind or cls["id"], "family": None, "guide": kind or cls["id"], "device": dev["id"], "ip": dev["ip"],
                "title": dev.get("title") or dev["ip"] or dev.get("mac") or "", "device_kind": dev.get("kind"),
                "certainty": "sure" if sure else "probable", "reasons": reasons,
                "prefill": setup_assist._prefill(kind, None, dev, None) if kind else {},
                "configured": bool(kind) and bool(dev["ip"]) and dev["ip"] in done.get(kind, set()),
                "entry": cls["id"], "status": cls["status"], "mode": cls["mode"], "benefit": cls["benefit"],
                "topic": cls.get("topic"), "guide_prefix": _guide_prefix(cls), "steps": cls["steps"]}
        out.append(item)
    return out


def suggestions(core, families=None) -> list:
    """Everything that can be connected, best first: not yet connected before connected, available before planned,
    then by benefit and certainty. One entry per class and device."""
    from .. import integrations as registry
    fams = {f.family: f for f in (list(families) if families is not None else registry.families(selectable_only=True))}
    by_id = {c["id"]: c for c in classes()}
    done = setup_assist._configured_hosts(core)
    items, seen = [], set()
    for s in setup_assist.suggestions(core, families=list(fams.values())):
        cls = _class_for(s, by_id)
        if cls is None:
            continue
        it = _decorate(s, cls, fams.get(s["family"]))
        items.append(it)
        seen.add((cls["id"], s["device"]))
        seen.add(("kind:" + s["kind"], s["device"]))
    for cls in by_id.values():
        if cls["source"] != "catalog":
            continue
        for it in _planned_or_extra(core, cls, done):
            same_kind = cls["status"] == "available" and ("kind:" + it["kind"], it["device"]) in seen
            if (cls["id"], it["device"]) not in seen and not same_kind:
                items.append(it)
                seen.add((cls["id"], it["device"]))
                if cls["status"] == "available":
                    seen.add(("kind:" + it["kind"], it["device"]))
    rank = {"sure": 0, "probable": 1}
    items.sort(key=lambda s: (s["configured"], s["status"] != "available", -s["benefit"], rank.get(s["certainty"], 2),
                              setup_assist.classify_ip_key(s["ip"] or ""), s["device"]))
    return items[:200]


def for_device(core, device_id: str) -> list:
    return [s for s in suggestions(core) if s["device"] == device_id]


def counts(items: list) -> dict:
    return {"available": sum(1 for s in items if s["status"] == "available" and not s["configured"]),
            "connected": sum(1 for s in items if s["configured"]),
            "planned": sum(1 for s in items if s["status"] == "planned")}
