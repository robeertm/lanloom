"""What the control room reads from a home-automation hub: entities with their current value, grouped by hub device.

Only entities the bridge already lets through (lanloom/hub_privacy.py) are in the snapshot; nothing is asked from the hub here.
"""
from __future__ import annotations

import unicodedata
from typing import Iterator, Optional


def fold(text) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def entities(core) -> Iterator[dict]:
    """{integ, device, device_name, area (id), area_name, entity_id, name, text (folded name + id), unit, dc, domain, state, t}"""
    hubs = getattr(core, "hub", None) or {}
    for integ_id, entry in list(hubs.items()):
        snap = entry.get("snapshot") or {}
        states = entry.get("states") or {}
        dev_names = {d.get("id"): d.get("name") for d in snap.get("devices") or []}
        area_names = {a.get("id"): a.get("name") for a in snap.get("areas") or [] if isinstance(a, dict)}
        for ent in snap.get("entities") or []:
            eid = ent.get("entity_id")
            if not isinstance(eid, str):
                continue
            st = states.get(eid)
            yield {"integ": integ_id, "device": ent.get("device_id"), "device_name": dev_names.get(ent.get("device_id")) or "",
                   "area": ent.get("area"), "area_name": area_names.get(ent.get("area")) or "",
                   "entity_id": eid, "name": ent.get("name") or "", "text": fold((ent.get("name") or "") + " " + eid.replace("_", " ")),
                   "unit": str(ent.get("unit") or "").strip(), "dc": ent.get("dc"), "domain": ent.get("domain") or eid.split(".", 1)[0],
                   "state": st[0] if st else ent.get("state"), "t": (st[1] if st else None) or snap.get("t")}


def as_number(value) -> Optional[float]:
    try:
        v = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) < 1e15 else None
