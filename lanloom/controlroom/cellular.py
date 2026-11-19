"""Mobile-router card: bearer/band table, signal quality at one place, reserve line, data volume (read only, vendor neutral).

Sources (whatever is there):

* ``cellular`` list of a controller family's state (``lanloom/firewall/neutral.py``: ``rsrp, rsrq, sinr, rssi`` in dBm/dB,
  ``tech``, ``bands``, ``carrier``, ``data_used``/``data_limit`` in bytes, ``active``, and for admins ``cell_id``, ``tac``, ``location``),
* entities of a home-automation hub whose names carry the usual words (RSRP, RSRQ, SINR, RSSI, band, network type, operator,
  data used / limit); several entities of one hub device make one card.

**Signal quality** has one table, ``SIGNAL``: for every measure the four limits between excellent / good / fair / poor / bad.
The card shows the worst measure (a good RSRP with a bad SINR is a bad link). Fair and better is ``ok``, poor ``warn``,
bad ``danger``; an idle reserve line is never worse than ``warn``.

**Bearer table** ``BANDS``: band name -> (duplex, centre MHz, role). Low bands carry coverage, high bands capacity.
Cell id, tracking area and position are admin data: they are in the answer only when the caller is an administrator.
"""
from __future__ import annotations

import re
from typing import Optional

from . import hubfacts
from .levels import freshness, num

# measure -> (excellent, good, fair, poor) lower limits; below the last one is "bad"
SIGNAL = {"rsrp": (-80.0, -90.0, -100.0, -110.0), "rsrq": (-10.0, -15.0, -20.0, -25.0), "sinr": (20.0, 13.0, 0.0, -5.0), "rssi": (-65.0, -75.0, -85.0, -95.0)}
GRADES = ("excellent", "good", "fair", "poor", "bad")
BANDS = {   # name -> (duplex, MHz, role)
    "B1": ("FDD", 2100, "capacity"), "B3": ("FDD", 1800, "capacity"), "B7": ("FDD", 2600, "capacity"), "B8": ("FDD", 900, "coverage"),
    "B20": ("FDD", 800, "coverage"), "B28": ("FDD", 700, "coverage"), "B32": ("SDL", 1500, "capacity"), "B38": ("TDD", 2600, "capacity"),
    "B40": ("TDD", 2300, "capacity"), "B41": ("TDD", 2500, "capacity"), "N1": ("FDD", 2100, "capacity"), "N3": ("FDD", 1800, "capacity"),
    "N28": ("FDD", 700, "coverage"), "N78": ("TDD", 3500, "capacity"), "N77": ("TDD", 3700, "capacity"),
}
BAND_RX = re.compile(r"\b(?:band\s*)?([bn])\s?(\d{1,3})\b", re.I)
TECH_WORDS = (("5g sa", "5G SA"), ("nr sa", "5G SA"), ("5g nsa", "5G NSA"), ("nr nsa", "5G NSA"), ("nsa", "5G NSA"), ("5g", "5G"), ("nr", "5G"),
              ("lte", "LTE"), ("4g", "LTE"), ("umts", "3G"), ("wcdma", "3G"), ("hspa", "3G"), ("3g", "3G"), ("edge", "2G"), ("gsm", "2G"), ("2g", "2G"))
DATA_WARN = 80.0


def grade_of(measure: str, value) -> Optional[int]:
    """0 excellent .. 4 bad for one measure, None when the measure or the value is unknown."""
    v, limits = num(value), SIGNAL.get(measure)
    if v is None or limits is None:
        return None
    for i, lim in enumerate(limits):
        if v >= lim:
            return i
    return 4


def quality(sig: dict) -> dict:
    """{grade, level, worst, measures: {name: grade}} from rsrp/rsrq/sinr/rssi (RSSI only when no other measure exists)."""
    grades = {m: grade_of(m, sig.get(m)) for m in ("rsrp", "rsrq", "sinr")}
    grades = {m: g for m, g in grades.items() if g is not None}
    if not grades:
        g = grade_of("rssi", sig.get("rssi"))
        if g is not None:
            grades["rssi"] = g
    if not grades:
        return {"grade": None, "level": "unknown", "worst": None, "measures": {}}
    worst = max(grades, key=lambda m: grades[m])
    g = grades[worst]
    return {"grade": GRADES[g], "level": "danger" if g >= 4 else "warn" if g == 3 else "ok", "worst": worst,
            "measures": {m: GRADES[v] for m, v in grades.items()}}


def parse_bands(value) -> list:
    out, seen = [], set()
    for kind, n in BAND_RX.findall(str(value or "")):
        name = "%s%s" % (kind.upper(), int(n))
        if name in seen:
            continue
        seen.add(name)
        duplex, mhz, role = BANDS.get(name, (None, None, None))
        out.append({"band": name if kind.upper() == "B" else name.lower(), "kind": "LTE" if kind.upper() == "B" else "NR", "duplex": duplex, "mhz": mhz, "role": role})
    return out[:8]


def tech_of(value) -> str:
    s = " " + hubfacts.fold(value) + " "
    for word, name in TECH_WORDS:
        if " " + word + " " in s or (word in ("5g sa", "5g nsa", "nr sa", "nr nsa") and word in s):
            return name
    return str(value or "")[:16]


def _card(base: dict, now: float, admin: bool) -> dict:
    sig = {k: num(base.get(k)) for k in ("rsrp", "rsrq", "sinr", "rssi")}
    q = quality(sig)
    used, limit = num(base.get("data_used")), num(base.get("data_limit"))
    pct = round(100.0 * used / limit, 1) if used is not None and limit and limit > 0 else None
    data_level = "unknown" if used is None else ("danger" if pct is not None and pct >= 100 else "warn" if pct is not None and pct >= DATA_WARN else "ok")
    age = max(0.0, now - base["t"]) if base.get("t") else None
    level = q["level"]
    if base.get("active") is not True and level == "danger":
        level = "warn"                                      # an idle reserve line that is weak is a hint, not an alarm
    bands = base.get("bands") if isinstance(base.get("bands"), list) and base["bands"] and isinstance(base["bands"][0], dict) else parse_bands(base.get("bands"))
    card = {"id": base["id"], "line": base.get("line") or "", "name": str(base.get("name") or "")[:64], "carrier": str(base.get("carrier") or "")[:48],
            "tech": tech_of(base.get("tech")), "bands": bands, "signal": sig, "quality": q, "level": level,
            "active": base.get("active") if isinstance(base.get("active"), bool) else None, "device": base.get("device"),
            "data": {"used": used, "limit": limit, "pct": pct, "level": data_level, "period_end_t": num(base.get("period_end_t"))},
            "age_s": None if age is None else round(age, 1), "fresh": freshness(age), "source": base.get("source") or ""}
    if admin:
        card["admin"] = {k: str(base.get(k))[:64] for k in ("cell_id", "tac", "location") if base.get(k) not in (None, "")}
    return card


# ---------------------------------------------------------------------------- readings from a hub
_HUB_MEASURES = (("rsrp", ("rsrp",)), ("rsrq", ("rsrq",)), ("sinr", ("sinr", "snr")), ("rssi", ("rssi", "signal strength")),
                 ("tech", ("network type", "access technology", "connection type", "radio technology", "rat")),
                 ("bands", ("band",)), ("carrier", ("operator", "carrier", "network name", "provider")),
                 ("data_used", ("data used", "data usage", "used data", "data consumed")), ("data_limit", ("data limit", "data quota", "data cap")),
                 ("cell_id", ("cell id", "cellid", "cell identity")), ("tac", ("tracking area", "tac")))
_UNIT_BYTES = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def _measure_of(ent: dict) -> Optional[str]:
    text = " " + ent["text"] + " "
    for key, words in _HUB_MEASURES:
        if any((" " + w + " ") in text or (len(w) > 4 and w in text) for w in words):
            return key
    return None


def from_hub(core, now: float) -> list:
    by: dict = {}
    for ent in hubfacts.entities(core):
        if ent["domain"] not in ("sensor", None) or not ent["device"]:
            continue
        m = _measure_of(ent)
        if m is None:
            continue
        key = (ent["integ"], ent["device"])
        g = by.setdefault(key, {"name": ent["device_name"], "vals": {}, "t": 0.0, "integ": ent["integ"], "device": ent["device"]})
        raw = ent["state"]
        if raw in (None, "unknown", "unavailable", ""):
            continue
        if m in ("tech", "bands", "carrier", "cell_id", "tac"):
            g["vals"][m] = str(raw)
        else:
            v = hubfacts.as_number(raw)
            if v is None:
                continue
            if m in ("data_used", "data_limit"):
                v *= _UNIT_BYTES.get(ent["unit"].lower(), 1)
            g["vals"][m] = v
        if ent["t"]:
            g["t"] = max(g["t"], float(ent["t"]))
    out = []
    for (integ, dev), g in by.items():
        vals = g["vals"]
        if not any(k in vals for k in ("rsrp", "rsrq", "sinr", "rssi")):
            continue                                                  # a router without any signal figure is no mobile router
        try:
            net = core.hub_merge()["by_hub"].get("%s:%s" % (integ, dev))
        except Exception:
            net = None
        out.append(dict(vals, id="hub:%s:%s" % (integ, dev), name=g["name"], t=g["t"] or None, source="hub", device=net, line=""))
    return out


def cards(core, now: float, admin: bool = False, lines: Optional[list] = None) -> list:
    """Every mobile-router card, the one on the active line first."""
    bases = []
    try:
        entries = core.live_configs()
    except Exception:
        entries = []
    for e in entries:
        for c in (e.get("state") or {}).get("cellular") or []:
            if isinstance(c, dict) and isinstance(c.get("id"), str):
                bases.append(dict(c, t=now - float(e.get("age_s") or 0), source="adapter"))
    bases += from_hub(core, now)
    by_line = {ln["id"]: ln for ln in lines or []}
    for b in bases:                                     # the line it belongs to tells whether the reserve is in use
        ln = by_line.get(b.get("line") or "")
        if ln is not None and b.get("active") is None:
            b["active"] = ln["active"] if ln["active"] is not None else None
    out = [_card(b, now, admin) for b in bases]
    out.sort(key=lambda c: (c["active"] is not True, c["name"].lower()))
    return out
