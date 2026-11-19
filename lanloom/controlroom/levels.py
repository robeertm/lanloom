"""Levels and freshness shared by the parts of the control room (one vocabulary: ok < warn < danger; unknown and none are not levels of risk)."""
from __future__ import annotations

from typing import Optional

ORDER = {"none": 0, "ok": 1, "unknown": 2, "warn": 3, "danger": 4}
FRESH_S = 180.0            # a reading younger than this is fresh
STALE_S = 900.0            # older than this the value is shown dimmed and counts as unknown, never as ok


def worst(levels) -> str:
    out = "none"
    for lv in levels:
        if ORDER.get(lv, 0) > ORDER[out]:
            out = lv
    return out


def freshness(age_s: Optional[float], fresh_s: float = FRESH_S, stale_s: float = STALE_S) -> str:
    """fresh | late | stale | none (no reading at all)."""
    if age_s is None:
        return "none"
    return "fresh" if age_s <= fresh_s else "late" if age_s <= stale_s else "stale"


def num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) < 1e15 else None


def pct_level(p: Optional[float], warn: float = 85.0, bad: float = 95.0) -> str:
    if p is None:
        return "unknown"
    return "danger" if p >= bad else "warn" if p >= warn else "ok"


def item(key: str, level: str = "ok", **vars) -> dict:
    return {"key": key, "vars": vars, "level": level}
