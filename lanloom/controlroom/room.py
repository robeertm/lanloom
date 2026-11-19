"""The control room: the blocks of the page and the list of what needs a look.

Blocks (each only when its source exists, so a small network sees a small page): ``gateway``, ``wan``, ``switch``, ``nas``,
``ups``, ``hub`` (home automation), ``dns`` and ``backups``. A block is::

    {"id", "level": ok|warn|danger|unknown, "fresh": fresh|late|stale|none, "age_s", "lines": [{key, vars, level}], "link": "#/..."}

``level`` is the worst line; a block whose newest reading is stale is ``unknown`` and dimmed, never ``ok``. Texts are
i18n keys with variables (``*_dur`` seconds, ``*_bytes``, ``*_at`` timestamps - the same convention as the nightly report).
``points`` are the lines that need a look (warn or danger), stable ids, worst first. ``sources`` is the freshness of every
connection Lanloom reads from - the page says how old the picture behind each block is.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from . import backups as backups_mod
from . import cellular, nas as nas_mod, ups as ups_mod, wan as wan_mod
from .levels import ORDER, freshness, item, num, worst

log = logging.getLogger("lanloom")
LOOP_S = 15.0
CACHE_S = 5.0


class ControlRoom:
    def __init__(self, core, clock=time.time):
        self.core = core
        self.clock = clock
        self.wan = wan_mod.WanWatch(core, clock)
        self.ups = ups_mod.UpsBook(core, clock)
        self.backups = backups_mod.BackupWatch(core, clock)
        self._cache = (0.0, None)

    # ------------------------------------------------------------------ live loop
    async def loop(self) -> None:
        while True:
            try:
                await self.wan.probe_if_due()
                self.wan.step()
                self.backups.chains()           # keeps the run tracker current (progress, learned duration)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("control room step failed")
            await asyncio.sleep(LOOP_S)

    def feed(self, integ_id, etype: str, ev: dict, now: float) -> None:
        """Called by the core for readings the control room cares about (``nut`` events, SNMP frames with a UPS-MIB)."""
        if etype == "nut":
            self.ups.feed("nut", integ_id, ev.get("host") or "", ev.get("units") or [], now)
        elif etype == "snmp" and ev.get("ups"):
            self.ups.feed("snmp", integ_id, ev.get("host") or "", [ev["ups"]], now)

    def forget_integration(self, integ_id) -> None:
        self.ups.forget(integ_id)

    # ------------------------------------------------------------------ blocks
    def _live(self, now: float) -> list:
        try:
            return self.core.live_configs(86400.0)
        except Exception:
            return []

    def _gateway(self, now: float, wan_view: dict) -> Optional[dict]:
        core = self.core
        lines, ages = [], []
        eq_found = False
        seen = set()
        for e in self._live(now):
            st = e.get("state") or {}
            for q in st.get("equipment") or []:
                if q.get("kind") != "router":
                    continue
                who = q.get("mgmt_ip") or q.get("name")
                if who in seen:                       # two connections describing the same gateway: one set of lines
                    continue
                seen.add(who)
                eq_found = True
                ages.append(float(e.get("age_s") or 0))
                stats = q.get("stats") or {}
                if q.get("online") is False:
                    lines.append(item("cr.gw.offline", "danger", name=q.get("name") or "?"))
                for metric, (warn, bad) in (("cpu", (85, 95)), ("mem", (85, 95)), ("temp", (75, 90))):
                    v = num(stats.get(metric))
                    if v is not None:
                        lv = "danger" if v >= bad else "warn" if v >= warn else "ok"
                        lines.append(item("cr.gw." + metric, lv, value=round(v)))
                if num(stats.get("uptime_s")) is not None:
                    lines.append(item("cr.gw.uptime", "ok", up_dur=int(stats["uptime_s"])))
            ids = st.get("ids")
            if isinstance(ids, dict) and ids.get("enabled") is not None:
                if not ids["enabled"]:
                    lines.append(item("cr.gw.ids_off", "warn"))
                elif num(ids.get("blocked_24h")) is not None:
                    lines.append(item("cr.gw.ids", "ok", n=int(ids["blocked_24h"])))
        if not eq_found:
            return None
        age = min(ages) if ages else None
        return self._block("gateway", lines, age, "#/network")

    def _switches(self, now: float) -> Optional[dict]:
        total = online = 0
        ages = []
        for e in self._live(now):
            st = e.get("state") or {}
            eq = [q for q in st.get("equipment") or [] if q.get("kind") == "switch"]
            if not eq:
                continue
            ages.append(float(e.get("age_s") or 0))
            total += len(eq)
            online += sum(1 for q in eq if q.get("online") is not False)
        if not total:
            return None
        lines = [item("cr.sw.count", "ok" if online == total else "danger" if online == 0 else "warn", online=online, total=total)]
        if online < total:
            lines.append(item("cr.sw.offline", "warn", n=total - online))
        return self._block("switch", lines, min(ages), "#/network")

    def _wan(self, now: float, wv: dict) -> Optional[dict]:
        if wv["state"] == "unknown" and not wv["lines"] and wv["axes"]["gateway"]["state"] == "unknown" and not wv["probe"]["configured"]:
            return None
        lines = []
        st = wv["state"]
        lines.append(item("cr.wan.state." + st, {"up": "ok", "down": "danger", "flapping": "warn", "unknown": "unknown"}[st],
                          since_at=wv["since_t"] or 0))
        if wv["disagree"] != "none":
            lines.append(item("cr.wan.disagree." + wv["disagree"], "warn"))
        fo = wv["failover"]
        if fo["on_reserve"]:
            lines.append(item("cr.wan.on_reserve", "warn", since_at=fo["since_t"] or 0))
        elif fo["has_reserve"]:
            lines.append(item("cr.wan.reserve_idle", "ok"))
        if fo["switches_24h"]:
            lines.append(item("cr.wan.switches", "warn" if fo["switches_24h"] > 2 else "ok", n=fo["switches_24h"]))
        lat = wv["axes"]["probe"].get("latency_ms")
        if lat is not None:
            lines.append(item("cr.wan.latency", "ok", ms=round(lat)))
        if not wv["probe"]["configured"]:
            lines.append(item("cr.wan.no_probe", "ok"))
        for c in wv.get("cellular") or []:
            if c["level"] in ("warn", "danger"):
                lines.append(item("cr.wan.signal", c["level"], name=c["name"] or "", grade_key="cr.grade." + (c["quality"]["grade"] or "none")))
            if c["data"]["level"] in ("warn", "danger"):
                lines.append(item("cr.wan.data", c["data"]["level"], name=c["name"] or "", pct=round(c["data"]["pct"] or 0)))
        ages = [a for a in (wv["axes"]["gateway"]["age_s"], wv["axes"]["probe"]["age_s"]) if a is not None]
        blk = self._block("wan", lines, min(ages) if ages else None, "#/wan")
        if st == "unknown":
            blk["level"] = "unknown"
        return blk

    def _nas(self, now: float, rows: list) -> Optional[dict]:
        if not rows:
            return None
        lines = []
        for c in rows:
            if not c["reading"]:
                lines.append(item("cr.nas.no_reading", "unknown", name=c["title"]))
                continue
            if c["volumes"]:
                v = c["volumes"][0]
                lines.append(item("cr.nas.volume", v["level"], name=c["title"], volume=v["name"], pct=round(v["pct"])))
            for k in ("temp",):
                if k in c["vitals"] and c["vitals"][k]["level"] != "ok":
                    lines.append(item("cr.nas.temp", c["vitals"][k]["level"], name=c["title"], value=round(c["vitals"][k]["value"])))
            if c["containers"] and c["containers"]["unhealthy"]:
                lines.append(item("cr.nas.containers", "warn", name=c["title"], n=c["containers"]["unhealthy"]))
        if not any(ln["level"] != "ok" for ln in lines):
            lines.insert(0, item("cr.nas.ok", "ok", n=len(rows)))
        ages = [s["age_s"] for c in rows for s in c["sources"] if s["fresh"] != "stale"]
        return self._block("nas", lines, min(ages) if ages else None, "#/nas")

    def _ups(self, now: float, units: list) -> Optional[dict]:
        if not units:
            return None
        lines = []
        for u in units:
            if u["state"] in ("battery", "low_battery"):
                lines.append(item("cr.ups." + u["state"], u["level"], name=u["name"], charge=round(u["charge"]) if u["charge"] is not None else None,
                                  runtime_dur=int(u["runtime_s"]) if u["runtime_s"] is not None else None))
            elif u["state"] in ("replace", "overload"):
                lines.append(item("cr.ups." + u["state"], "warn", name=u["name"]))
            elif u["state"] == "unknown":
                lines.append(item("cr.ups.unknown", "unknown", name=u["name"]))
            else:
                lines.append(item("cr.ups.online", "ok", name=u["name"], charge=round(u["charge"]) if u["charge"] is not None else None,
                                  runtime_dur=int(u["runtime_s"]) if u["runtime_s"] is not None else None, load=round(u["load"]) if u["load"] is not None else None))
        ages = [u["age_s"] for u in units]
        return self._block("ups", lines, min(ages), "#/controlroom")

    def _hub(self, now: float) -> Optional[dict]:
        core = self.core
        hubs = getattr(core, "hub", None) or {}
        if not hubs:
            return None
        lines, ages = [], []
        for integ_id, entry in hubs.items():
            snap = entry.get("snapshot") or {}
            t = snap.get("t")
            age = max(0.0, now - t) if t else None
            cad = core.cadence.get("int:%d" % integ_id)
            last = getattr(cad, "last", None)
            if last:
                age = max(0.0, now - last) if age is None else min(age, max(0.0, now - last))
            if age is not None:
                ages.append(age)
            if not snap:
                lines.append(item("cr.hub.waiting", "unknown"))
                continue
            lines.append(item("cr.hub.connected", "ok" if (age or 0) <= 900 else "unknown", n=len(snap.get("entities") or [])))
        for k in ("automation_failed",):
            pass
        return self._block("hub", lines, min(ages) if ages else None, "#/sensors")

    def _dns(self, now: float) -> Optional[dict]:
        from .. import dnsfilter
        try:
            rows = dnsfilter.overview(self.core, now)
        except Exception:
            return None
        if not rows:
            return None
        lines = []
        for r in rows:
            lv = {"ok": "ok", "warn": "warn", "danger": "danger", "bad": "danger"}.get(str(r.get("health") or "ok"), "ok")
            lines.append(item("cr.dns.line", lv, name=r["name"], queries=r.get("queries") or 0, share=round((r.get("share") or 0) * 100)))
        return self._block("dns", lines, 0.0, "#/network")

    def _backups(self, now: float, view: dict) -> Optional[dict]:
        if not view["chains"]:
            return None
        sm = view["summary"]
        lines = []
        bad = sm.get("failed", 0)
        if bad:
            lines.append(item("cr.bk.failed", "danger", n=bad))
        if sm.get("late", 0):
            lines.append(item("cr.bk.late", "warn", n=sm["late"]))
        if sm.get("none", 0):
            lines.append(item("cr.bk.none", "warn", n=sm["none"]))
        if sm.get("unknown", 0):
            lines.append(item("cr.bk.unknown", "unknown", n=sm["unknown"]))
        if sm.get("running", 0):
            lines.append(item("cr.bk.running", "ok", n=sm["running"]))
        if not lines or sm.get("ok", 0):
            lines.append(item("cr.bk.ok", "ok", n=sm.get("ok", 0), total=len(view["chains"])))
        return self._block("backups", lines, 0.0, "#/backups")

    @staticmethod
    def _block(bid: str, lines: list, age: Optional[float], link: str) -> dict:
        fr = freshness(age)
        level = worst(ln["level"] for ln in lines) if lines else "unknown"
        if fr == "stale":
            level = "unknown"
        return {"id": bid, "level": "unknown" if level == "none" else level, "fresh": fr, "age_s": None if age is None else round(age, 1), "lines": lines, "link": link}

    # ------------------------------------------------------------------ the page
    def snapshot(self, now: Optional[float] = None, admin: bool = False) -> dict:
        now = self.clock() if now is None else now
        wv = self.wan.view(now)
        wv["cellular"] = cellular.cards(self.core, now, admin, wv["lines"])
        bv = self.backups.view(now)
        units = self.ups.view(now)
        nas_rows = nas_mod.cards(self.core, now, bv["chains"], self.ups)
        blocks = [b for b in (self._gateway(now, wv), self._wan(now, wv), self._switches(now), self._nas(now, nas_rows), self._ups(now, units),
                              self._hub(now), self._dns(now), self._backups(now, bv)) if b]
        points = []
        for b in blocks:
            for ln in b["lines"]:
                if ln["level"] in ("warn", "danger"):
                    points.append({"id": b["id"] + ":" + ln["key"], "block": b["id"], "key": ln["key"], "vars": ln["vars"], "level": ln["level"]})
        points.sort(key=lambda p: -ORDER[p["level"]])
        level = worst(b["level"] for b in blocks) if blocks else "none"
        return {"t": now, "level": level, "blocks": blocks, "points": points, "sources": self._sources(now),
                "wan": wv, "nas": nas_rows, "ups": units, "backups": bv}

    def _sources(self, now: float) -> list:
        out = []
        for s in self.core.source_list():
            age = s.get("age_s")
            out.append({"id": s["id"], "name": s["name"], "kind": s.get("kind") or s["id"], "grade": s["grade"], "age_s": age,
                        "fresh": freshness(age)})
        return out

    def summary(self, now: Optional[float] = None) -> dict:
        """Cheap figures for the overview, the palette and the nightly report (no probe, no cards)."""
        now = self.clock() if now is None else now
        bv = self.backups.view(now)
        return {"wan": self.wan.view(now)["state"], "ups": [u["state"] for u in self.ups.view(now)], "backups": bv["summary"]}
