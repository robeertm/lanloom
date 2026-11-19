"""Storage-box card: ring, volumes, containers, virtual machines, backups and battery backup in one place (read only, vendor neutral).

For every device Lanloom knows as a storage box (kind ``nas``, set by classification or by a person) the card joins what the
sources deliver anyway:

* ``snmp``       - fixed disks of HOST-RESOURCES-MIB (every volume with used/total), memory, processor, temperature,
* ``agent``      - mounts of the host agent and its containers,
* ``hypervisor`` - storage and guests when the box also runs virtual machines,
* ``hub``        - system sensors of a hub device merged with the box,
* the backup chains of the box (``backups.py``) and the battery backup that carries it (``ups.py``).

**Ring:** the share of the fullest volume (green below 85 %, yellow from 85 %, red from 95 %); the ring's colour is the worst
of the volumes, the vitals (temperature, memory) and the freshness of the sources - a silent source is dimmed, never red, and
a card without any reading says "no reading" instead of looking healthy. Nothing is shown that no source told.
"""
from __future__ import annotations

from typing import Optional

from .levels import freshness, num, pct_level, worst

MIN_VOLUME = 256 << 20
SKIP_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/boot", "/snap", "/var/lib/docker", "/var/lib/kubelet")
MAX_ITEMS = 12
VITAL_LEVELS = {"temp": (70.0, 85.0), "mem": (85.0, 95.0), "cpu": (85.0, 95.0)}


def _skipped(name) -> bool:
    """A system mount (the mount itself or anything below it; ``/run`` is not ``/runner-data``)."""
    n = str(name or "")
    return any(n == m or n.startswith(m + "/") for m in SKIP_MOUNTS)


def is_nas(dev: dict) -> bool:
    return (dev.get("user_kind") or dev.get("kind")) == "nas"


def _vol(name, used, total, source) -> Optional[dict]:
    used, total = num(used), num(total)
    if not total or total < MIN_VOLUME or used is None or used < 0:
        return None
    pct = round(max(0.0, min(100.0, 100.0 * used / total)), 1)
    return {"name": str(name)[:64], "used": used, "total": total, "pct": pct, "level": pct_level(pct), "source": source}


def volumes(core, dev_id: str, now: float, readings_ip: str) -> list:
    out, seen = [], set()

    def add(v):
        if v and v["name"] not in seen:
            seen.add(v["name"])
            out.append(v)
    agents = getattr(core, "agents", None)
    if agents is not None:
        try:
            detail = agents.device_detail(dev_id, now) or {}
        except Exception:
            detail = {}
        for h in detail.get("hosts") or []:
            for d in (h.get("system") or {}).get("disks") or []:
                m = str(d.get("mount") or "")
                if m and not _skipped(m) and d.get("total"):
                    add(_vol(m, (num(d.get("total")) or 0) - (num(d.get("free")) or 0), d.get("total"), "agent"))
    for ev in list((getattr(core, "snmp", None) or {}).values()):
        if core.ip_to_device.get(ev.get("host") or "") == dev_id:
            for v in (ev.get("hr") or {}).get("volumes") or []:
                if not _skipped(v.get("name")):
                    add(_vol(v["name"], v.get("used"), v.get("total"), "snmp"))
    try:
        entries = core.live_configs()
    except Exception:
        entries = []
    for e in entries:
        st = e.get("state") or {}
        for h in st.get("hosts") or []:
            if core.ip_to_device.get(h.get("ip") or "") == dev_id:
                for s in st.get("storage") or []:
                    if s.get("host") == h.get("id"):
                        add(_vol(s.get("name") or s.get("id"), s.get("used"), s.get("total"), "hypervisor"))
    out.sort(key=lambda v: (-v["pct"], v["name"]))
    return out


def containers(core, dev_id: str, now: float) -> Optional[dict]:
    agents = getattr(core, "agents", None)
    if agents is None:
        return None
    try:
        rows = agents.containers_by_device(now).get(dev_id) or []
    except Exception:
        return None
    if not rows:
        return None
    items = [{"name": str(c.get("name") or "?")[:48], "state": c.get("state") or "", "health": c.get("health") or ""} for _a, c in rows]
    running = sum(1 for i in items if i["state"] == "running")
    bad = sum(1 for i in items if i["health"] == "unhealthy" or i["state"] == "restarting")
    return {"total": len(items), "running": running, "unhealthy": bad, "items": items[:MAX_ITEMS],
            "level": "warn" if bad else "ok", "more": max(0, len(items) - MAX_ITEMS)}


def guests(core, dev_id: str) -> Optional[dict]:
    items = []
    try:
        entries = core.live_configs()
    except Exception:
        return None
    for e in entries:
        st = e.get("state") or {}
        host_ids = {h["id"] for h in st.get("hosts") or [] if core.ip_to_device.get(h.get("ip") or "") == dev_id}
        for g in st.get("guests") or []:
            if g.get("host") in host_ids:
                items.append({"name": str(g.get("name") or "?")[:48], "kind": g.get("kind") or "vm", "status": g.get("status") or ""})
    if not items:
        return None
    running = sum(1 for i in items if i["status"] == "running")
    return {"total": len(items), "running": running, "items": items[:MAX_ITEMS], "more": max(0, len(items) - MAX_ITEMS)}


def vitals(readings: dict) -> dict:
    """Newest value of cpu / mem / temp over all sources of the device, with the source and the age."""
    out = {}
    for metric in ("cpu", "mem", "temp"):
        best = None
        for src, r in readings.items():
            v = num((r.get("values") or {}).get(metric))
            if v is not None and (best is None or r["t"] > best[1]):
                best = (v, r["t"], src)
        if best:
            warn, bad = VITAL_LEVELS[metric]
            out[metric] = {"value": round(best[0], 1), "source": best[2], "level": "danger" if best[0] >= bad else "warn" if best[0] >= warn else "ok"}
    return out


def card(core, dev: dict, now: float, readings: dict, backup_rows: list, ups_row: Optional[dict]) -> dict:
    dev_id = dev["id"]
    srcs = []
    for name, r in sorted(readings.items()):
        age = max(0.0, now - r["t"])
        srcs.append({"source": name, "age_s": round(age, 1), "fresh": freshness(age)})
    fresh_srcs = [s for s in srcs if s["fresh"] != "stale"]
    vols = volumes(core, dev_id, now, dev.get("ip") or "")
    vit = vitals({k: v for k, v in readings.items() if now - v["t"] <= 900})
    ct = containers(core, dev_id, now)
    vm = guests(core, dev_id)
    mine = [b for b in backup_rows if b.get("device") == dev_id]
    ring_pct = vols[0]["pct"] if vols else None
    levels = [v["level"] for v in vols] + [x["level"] for x in vit.values()]
    if ct:
        levels.append(ct["level"])
    levels += [b["level"] for b in mine if b["level"] in ("warn", "danger")]
    if ups_row and ups_row["level"] in ("warn", "danger"):
        levels.append(ups_row["level"])
    level = worst(levels) if levels else "unknown"
    if level == "ok" and not fresh_srcs:
        level = "unknown"
    return {"device": dev_id, "title": dev.get("user_title") or dev.get("title") or dev.get("ip") or dev_id, "ip": dev.get("ip") or "",
            "level": level, "ring": {"pct": ring_pct, "level": pct_level(ring_pct) if ring_pct is not None else "unknown",
                                     "volume": vols[0]["name"] if vols else ""},
            "volumes": vols, "vitals": vit, "containers": ct, "guests": vm, "backups": mine, "ups": ups_row, "sources": srcs,
            "reading": bool(fresh_srcs or vols)}


def cards(core, now: float, backup_rows: list, ups_book) -> list:
    try:
        readings_all = core.hosthealth.readings(now)
    except Exception:
        readings_all = {}
    out = []
    for dev in core.store.devices(include_hidden=False):
        if not is_nas(dev) or core.is_merged(dev["id"]):      # a second network port of a box is not a box of its own
            continue
        ups_row = ups_book.runtime_for(dev["id"], now) if ups_book is not None else None
        out.append(card(core, dev, now, readings_all.get(dev["id"]) or {}, backup_rows, ups_row))
    order = {"danger": 0, "warn": 1, "unknown": 2, "ok": 3}
    out.sort(key=lambda c: (order.get(c["level"], 4), c["title"].lower()))
    return out
