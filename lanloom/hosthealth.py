"""Host health and early warnings: how a host is doing right now, and what will run out soon (read only).

**Sources** (whatever is there; one device may have several):

* ``agent`` - the optional host agent (processor use, load, memory, swap, disks, temperature, throttling),
* ``snmp`` - an SNMP connection whose agent serves HOST-RESOURCES-MIB (processor, memory, swap, fixed disks; load and
  temperature where the agent offers the UCD/LM-SENSORS tables),
* ``hypervisor`` - a hypervisor connection (processor, memory, swap, load and the node's storage),
* ``controller`` - a network controller connection (processor and memory of routers, switches and access points),
* ``hub`` - system sensors of a home-automation hub on the hub device that is merged with the network device
  (processor use, memory, swap, disk use, load, processor temperature; matched by unit and name, nothing is guessed
  beyond that).

Every five minutes the newest reading of each source is written as one sample (``host_samples``, 35 days). Nothing is
asked from the network for this: the values are the ones the sources already deliver.

**Gauges** (the host card): processor, memory, disk, temperature, swap, load per core and the processor clock where
throttling is measurable. Colours: green below 70 %, yellow 70-90 %, red above 90 % (temperature: 70 / 85 °C; load per
core: 70 / 100 %). A source that stopped delivering is shown dimmed as "stale · x min ago", never red. The health ring
shows the worst gauge.

**Early warnings** (``lanloom.forecast``): memory, swap, disk, load and temperature as a trend over 6 hours and 7 days.
A limit reached in less than 72 hours with at least 3 support points is a warning: calm on the host card, one line in
the overview, a section of the nightly report and the notification event ``host_forecast``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Optional

from . import forecast

log = logging.getLogger("lanloom")

SAMPLE_S = 300
KEEP_DAYS = 35
STALE_S = 900                     # three missed samples: the source is stale (dimmed, not red)
SOURCES = ("agent", "snmp", "hypervisor", "controller", "hub")
METRICS = ("cpu", "mem", "disk", "temp", "swap", "load", "freq")
TREND_METRICS = ("mem", "swap", "disk", "load", "temp")
# value that counts as "full" (load: percent of the cores, 200 % = twice as many waiting tasks as cores)
LIMITS = {"mem": 95.0, "swap": 100.0, "disk": 95.0, "load": 200.0, "temp": 85.0}
LEVELS = {"temp": (70.0, 85.0), "load": (70.0, 100.0)}     # (yellow from, red above); everything else 70 / 90
DEFAULT_LEVELS = (70.0, 90.0)
HINTS_TTL = 300.0
MAX_HINTS = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS host_samples (slot INTEGER NOT NULL, device TEXT NOT NULL, source TEXT NOT NULL, cpu REAL, mem REAL,
    swap REAL, load REAL, cores INTEGER, disk REAL, temp REAL, freq REAL, throttled INTEGER, PRIMARY KEY (slot, device, source));
CREATE INDEX IF NOT EXISTS host_samples_dev ON host_samples(device, slot);
"""
COLUMNS = ("cpu", "mem", "swap", "load", "cores", "disk", "temp", "freq", "throttled")


def level_of(metric: str, value) -> str:
    v = forecast.num(value)
    if v is None:
        return "none"
    if metric == "freq":
        return "ok"
    warn, bad = LEVELS.get(metric, DEFAULT_LEVELS)
    return "danger" if v > bad else "warn" if v >= warn else "ok"


def _pct(used, total) -> Optional[float]:
    u, t = forecast.num(used), forecast.num(total)
    if u is None or not t or t <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * u / t)), 1)


def _clean(r: dict) -> dict:
    out = {}
    for k in COLUMNS:
        v = r.get(k)
        if k == "throttled":
            out[k] = None if v is None else bool(v)
        elif k == "cores":
            out[k] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 10000 else None
        else:
            f = forecast.num(v)
            out[k] = round(f, 2) if f is not None and -50 <= f <= 100000 else None
    return out


# ---------------------------------------------------------------------- readings per source
def from_agent(system: dict, cpus=None) -> dict:
    sys_ = system or {}
    disk = None
    for d in sys_.get("disks") or []:
        share = _pct((d.get("total") or 0) - (d.get("free") or 0), d.get("total"))
        if share is not None:
            disk = share if disk is None else max(disk, share)
    mem = None
    if sys_.get("mem_total") and sys_.get("mem_avail") is not None:
        mem = _pct(sys_["mem_total"] - sys_["mem_avail"], sys_["mem_total"])
    swap = None
    if sys_.get("swap_total") and sys_.get("swap_free") is not None:
        swap = _pct(sys_["swap_total"] - sys_["swap_free"], sys_["swap_total"])
    load = sys_.get("load") or []
    return _clean({"cpu": sys_.get("cpu"), "mem": mem, "swap": swap, "load": load[2] if len(load) > 2 else (load[0] if load else None),
                   "cores": cpus, "disk": disk, "temp": sys_.get("temp"), "freq": sys_.get("freq_pct"), "throttled": sys_.get("throttled")})


def from_snmp(hr: dict) -> dict:
    hr = hr or {}
    return _clean({k: hr.get(k) for k in ("cpu", "mem", "swap", "load", "cores", "disk", "temp")})


def from_hypervisor(host: dict, storage: list) -> dict:
    cpu = forecast.num(host.get("cpu"))
    disk = None
    for s in storage or []:
        share = _pct(s.get("used"), s.get("total"))
        if share is not None and (s.get("total") or 0) >= 256 << 20:
            disk = share if disk is None else max(disk, share)
    load = host.get("load") or []
    return _clean({"cpu": round(cpu * 100.0, 1) if cpu is not None and cpu <= 1.0 else cpu, "mem": _pct(host.get("mem_used"), host.get("mem_total")),
                   "swap": _pct(host.get("swap_used"), host.get("swap_total")) if host.get("swap_total") else None,
                   "load": load[2] if len(load) > 2 else None, "cores": host.get("cores"), "disk": disk})


def from_controller(stats: dict) -> dict:
    stats = stats or {}
    return _clean({"cpu": stats.get("cpu"), "mem": stats.get("mem"), "temp": stats.get("temp")})


_HUB_RULES = (       # metric, unit kind, words in the name or entity id (all lower case, without accents)
    ("temp", "temp", ("cpu", "processor", "soc", "core", "package", "system")),
    ("swap", "pct", ("swap",)),
    ("cpu", "pct", ("processor", "cpu")),
    ("mem", "pct", ("memory", "ram")),
    ("disk", "pct", ("disk", "storage", "volume", "drive")),
    ("load", "plain", ("load_15", "load 15", "load (15", "load15")),
)
_NOT_HUB = ("battery", "humidity", "signal", "free", "available", "frequency", "fan", "outside", "room")


def hub_metric(entity: dict) -> Optional[str]:
    """Which host value one hub entity is (or None). Only numeric sensors with a fitting unit; a free/available
    figure is never read as use."""
    if (entity or {}).get("domain") not in ("sensor", None):
        return None
    unit = str(entity.get("unit") or "").strip().lower()
    name = (str(entity.get("name") or "") + " " + str(entity.get("entity_id") or "")).lower().replace("_", " ")
    if any(w in name for w in _NOT_HUB):
        return None
    kind = "pct" if unit == "%" else "temp" if unit in ("°c", "c") or entity.get("dc") == "temperature" else "plain" if not unit else ""
    for metric, want, words in _HUB_RULES:
        raw_name = name.replace(" ", "_") + " " + name
        if want == kind and any(w in raw_name for w in words):
            if metric == "temp" and unit not in ("°c", "c"):
                return None
            return metric
    return None


class HostHealth:
    def __init__(self, core, clock=time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self._lock = threading.RLock()
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.store.execute(stmt)
        self._last_slot = None
        self._hints = (0.0, [])
        self._open: set = set()          # notification keys announced while the warning lasts
        self._fc_cache: dict = {}        # device -> (sample slot, forecasts)

    # ------------------------------------------------------------------ readings
    def readings(self, now: Optional[float] = None) -> dict:
        """{device id: {source: {"t", "values": {...}, "label"}}} - the newest reading of every source."""
        now = self.clock() if now is None else now
        core = self.core
        out: dict = {}

        def put(dev, source, t, values, label=""):
            if not dev or not t or not any(values.get(k) is not None for k in ("cpu", "mem", "swap", "load", "disk", "temp", "freq")):
                return
            have = out.setdefault(dev, {}).get(source)
            if have is None or have["t"] < t:
                out[dev][source] = {"t": float(t), "values": values, "label": str(label or "")[:64]}

        agents = getattr(core, "agents", None)
        if agents is not None:
            facts = {}
            try:
                facts = {r["id"]: json.loads(r["facts"] or "{}") for r in self.store.query("SELECT id, facts FROM agent_hosts")}
            except Exception:
                pass
            for aid, st in list(agents.live.items()):
                if not st.get("system") or not st.get("t"):
                    continue
                f = facts.get(aid) or {}
                put(agents.device_of(aid), "agent", st["t"], from_agent(st["system"], f.get("cpus")), f.get("name") or aid)
        for integ_id, ev in list((getattr(core, "snmp", None) or {}).items()):
            hr = ev.get("hr")
            if hr:
                put(core.ip_to_device.get(ev.get("host") or ""), "snmp", ev.get("hr_t") or ev.get("t"), from_snmp(hr), (ev.get("sys") or {}).get("name"))
        try:
            entries = core.live_configs()
        except Exception:
            entries = []
        for e in entries:
            st = e.get("state") or {}
            t = now - float(e.get("age_s") or 0)
            for h in st.get("hosts") or []:
                if h.get("online") is False:
                    continue
                dev = core.ip_to_device.get(h.get("ip") or "")
                storage = [s for s in st.get("storage") or [] if s.get("host") == h.get("id")]
                put(dev, "hypervisor", t, from_hypervisor(h, storage), h.get("name"))
            for q in st.get("equipment") or []:
                if q.get("online") is False or not q.get("stats"):
                    continue
                put(core.ip_to_device.get(q.get("mgmt_ip") or q.get("ip") or ""), "controller", t, from_controller(q["stats"]), q.get("name"))
        self._hub_readings(put, now)
        return out

    def _hub_readings(self, put, now: float) -> None:
        core = self.core
        hubs = getattr(core, "hub", None) or {}
        if not hubs:
            return
        try:
            by_hub = core.hub_merge()["by_hub"]
        except Exception:
            return
        for integ_id, entry in list(hubs.items()):
            snap = entry.get("snapshot") or {}
            states = entry.get("states") or {}
            names = {d.get("id"): d.get("name") for d in snap.get("devices") or []}
            per: dict = {}
            for ent in snap.get("entities") or []:
                did = ent.get("device_id")
                net = by_hub.get("%s:%s" % (integ_id, did)) if did else None
                if not net:
                    continue
                metric = hub_metric(ent)
                if not metric:
                    continue
                st = states.get(ent.get("entity_id"))
                v = forecast.num(st[0] if st else ent.get("state"))
                if v is None:
                    continue
                t = (st[1] if st else None) or snap.get("t") or now
                vals, ts, label = per.setdefault(net, ({}, [], names.get(did) or ""))
                if metric == "disk":
                    vals[metric] = max(v, vals.get(metric) or v)
                elif metric not in vals:
                    vals[metric] = v
                ts.append(float(t))
            for net, (vals, ts, label) in per.items():
                # a hub sends a state only when it changes: a quiet sensor is not a stale one, the bridge's freshness counts
                t = max(max(ts), float(entry.get("t") or 0), now - 60 if entry.get("snapshot") else 0)
                put(net, "hub", t, _clean(vals), label)

    # ------------------------------------------------------------------ samples
    def maybe_sample(self, now: Optional[float] = None) -> int:
        """Write one sample per device and source (at most once per 5-minute slot). Returns the rows written."""
        now = self.clock() if now is None else now
        slot = int(now // SAMPLE_S)
        if slot == self._last_slot:
            return 0
        self._last_slot = slot
        rows = []
        for dev, per in self.readings(now).items():
            for source, r in per.items():
                if now - r["t"] > STALE_S:
                    continue
                v = r["values"]
                rows.append((slot, dev, source) + tuple(int(v[k]) if k == "throttled" and v[k] is not None else v[k] for k in COLUMNS))
        if rows:
            with self.store.transaction() as st:
                st._db.executemany("INSERT OR REPLACE INTO host_samples(slot, device, source, %s) VALUES(?,?,?,%s)"
                                   % (", ".join(COLUMNS), ",".join("?" * len(COLUMNS))), rows)
        self._hints = (0.0, self._hints[1])
        self._fc_cache.clear()
        return len(rows)

    def invalidate(self) -> None:
        """Forget the cached trends and warnings (after samples were written from outside, e.g. the demo's history)."""
        self._fc_cache.clear()
        self._hints = (0.0, [])

    def prune(self, now: Optional[float] = None) -> int:
        now = self.clock() if now is None else now
        cur = self.store.execute("DELETE FROM host_samples WHERE slot < ?", (int((now - KEEP_DAYS * 86400) // SAMPLE_S),))
        return getattr(cur, "rowcount", 0) or 0

    def forget(self, device_id: str) -> None:
        self.store.execute("DELETE FROM host_samples WHERE device=?", (str(device_id),))
        self._fc_cache.pop(str(device_id), None)

    def rename(self, old: str, new: str) -> None:
        self.store.execute("UPDATE OR IGNORE host_samples SET device=? WHERE device=?", (new, old))
        self.forget(old)

    def _samples(self, dev_id: str, now: float, days: float = 7.0) -> list:
        return self.store.query("SELECT * FROM host_samples WHERE device=? AND slot>=? ORDER BY slot",
                                (str(dev_id), int((now - days * 86400) // SAMPLE_S)))

    # ------------------------------------------------------------------ forecasts
    def forecasts(self, dev_id: str, now: Optional[float] = None, rows: Optional[list] = None) -> list:
        """Trend of every trend metric of one device (the source with the most samples per metric). Without ``rows`` the
        answer is kept until the next sample slot: the samples, and so the trend, change only every five minutes."""
        now = self.clock() if now is None else now
        key = (str(dev_id), int(now // SAMPLE_S))
        if rows is None:
            hit = self._fc_cache.get(key[0])
            if hit and hit[0] == key[1]:
                return hit[1]
            out = self._forecasts(self._samples(dev_id, now), now)
            if len(self._fc_cache) > 500:
                self._fc_cache.clear()
            self._fc_cache[key[0]] = (key[1], out)
            return out
        return self._forecasts(rows, now)

    def _forecasts(self, rows: list, now: float) -> list:
        out = []
        for metric in TREND_METRICS:
            series: dict = {}
            for r in rows:
                v = r[metric]
                if v is None:
                    continue
                if metric == "load":
                    if not r["cores"]:
                        continue
                    v = 100.0 * v / r["cores"]
                series.setdefault(r["source"], []).append((r["slot"] * SAMPLE_S, v))
            if not series:
                continue
            source = max(series, key=lambda s: (len(series[s]), -SOURCES.index(s) if s in SOURCES else -99))
            pts = series[source]
            limit = LIMITS.get(metric)
            short = forecast.judge(pts, now, "6h", limit, step=SAMPLE_S)
            long = forecast.judge(pts, now, "7d", limit, step=SAMPLE_S)
            best = forecast.combine(short, long)
            best.update(metric=metric, source=source, limit=limit, current=round(pts[-1][1], 1), current_t=pts[-1][0])
            out.append(best)
        return out

    def hints(self, now: Optional[float] = None, names: Optional[dict] = None) -> list:
        """Every warning of every device (cached for five minutes): [{device, title, kind, metric, eta_h, ...}]."""
        now = self.clock() if now is None else now
        with self._lock:
            if self._hints[0] and now - self._hints[0] < HINTS_TTL:
                return self._hints[1]
            out = []
            since = int((now - 7 * 86400) // SAMPLE_S)
            devs = [r["device"] for r in self.store.query("SELECT DISTINCT device FROM host_samples WHERE slot>=?", (since,))]
            names = names if names is not None else self._names()
            for dev in devs:
                if dev not in names:
                    continue
                for f in self.forecasts(dev, now):
                    if f["level"] != "warn":
                        continue
                    out.append({"device": dev, "title": names[dev]["title"], "kind": names[dev]["kind"], "metric": f["metric"],
                                "eta_h": f["eta_h"], "current": f["current"], "limit": f["limit"], "slope_h": f["slope_h"],
                                "window": f["window"], "source": f["source"], "n": f["n"]})
            out.sort(key=lambda x: (x["eta_h"] if x["eta_h"] is not None else 1e9, x["title"]))
            out = out[:MAX_HINTS]
            self._hints = (now, out)
            return out

    def _names(self) -> dict:
        out = {}
        for dev in self.store.devices(include_hidden=False):
            out[dev["id"]] = {"title": dev.get("user_title") or dev["title"] or dev["ip"] or dev["id"],
                              "kind": dev.get("user_kind") or dev["kind"] or "unknown"}
        return out

    def announce(self, now: Optional[float] = None) -> None:
        """Notification event ``host_forecast``: once per warning while it lasts, cleared silently when it is gone."""
        notify = getattr(self.core, "notify", None)
        if notify is None:
            return
        now = self.clock() if now is None else now
        cur = set()
        for hnt in self.hints(now):
            key = "host_forecast:%s:%s" % (hnt["device"], hnt["metric"])
            cur.add(key)
            if key not in self._open:
                notify.emit("host_forecast", key, {"name": hnt["title"], "metric_key": "hh.metric." + hnt["metric"],
                                                   "eta_dur": round((hnt["eta_h"] or 0) * 3600)}, device=hnt["device"], t=now)
        for key in self._open - cur:
            notify.emit("host_forecast", key, resolve=True, t=now)
        self._open = cur

    # ------------------------------------------------------------------ the host card
    def card(self, dev_id: str, now: Optional[float] = None) -> dict:
        """Host card of one device: ring, gauges, sources with their freshness, forecasts and a 24-hour line per gauge."""
        now = self.clock() if now is None else now
        per = self.readings(now).get(dev_id) or {}
        rows = self._samples(dev_id, now, days=1.0)             # the 24-hour lines; the trends come from the cache
        lasts = {r["source"]: r for r in self.store.query(
            "SELECT s.* FROM host_samples s JOIN (SELECT source, MAX(slot) AS m FROM host_samples WHERE device=? AND slot>=? GROUP BY source) x"
            " ON s.source=x.source AND s.slot=x.m WHERE s.device=?", (str(dev_id), int((now - 7 * 86400) // SAMPLE_S), str(dev_id)))}
        if not per and not lasts:
            return {"known": False}
        sources = []
        for source in SOURCES:
            r = per.get(source)
            if r is None:
                last = lasts.get(source)
                if last is None:
                    continue
                r = {"t": last["slot"] * SAMPLE_S + SAMPLE_S, "values": {k: last[k] for k in COLUMNS}, "label": ""}
            age = max(0.0, now - r["t"])
            sources.append({"source": source, "label": r["label"], "age_s": round(age), "stale": age > STALE_S, "values": r["values"]})
        fresh = [s for s in sources if not s["stale"]]
        pick_from = fresh or sources
        gauges = []
        for metric in METRICS:
            for s in pick_from:
                v = s["values"].get(metric)
                if metric == "load" and v is not None:
                    cores = s["values"].get("cores")
                    if not cores:
                        continue
                    gauges.append({"metric": metric, "value": round(100.0 * v / cores, 1), "raw": v, "cores": cores, "source": s["source"],
                                   "stale": s["stale"], "age_s": s["age_s"], "level": "stale" if s["stale"] else level_of(metric, 100.0 * v / cores)})
                    break
                if v is not None:
                    g = {"metric": metric, "value": v, "source": s["source"], "stale": s["stale"], "age_s": s["age_s"],
                         "level": "stale" if s["stale"] else level_of(metric, v)}
                    if metric == "freq":
                        g["throttled"] = s["values"].get("throttled")
                        if g["throttled"] and not s["stale"]:
                            g["level"] = "warn"
                    gauges.append(g)
                    break
        ranked = [g for g in gauges if g["level"] in ("ok", "warn", "danger") and g["metric"] != "freq"]
        rank = {"ok": 0, "warn": 1, "danger": 2}
        if ranked:
            worst = max(ranked, key=lambda g: (rank[g["level"]], g["value"] / (LIMITS.get(g["metric"]) or 100.0)))
            ring = {"level": worst["level"], "metric": worst["metric"], "value": worst["value"],
                    "share": round(min(100.0, 100.0 * worst["value"] / (LIMITS.get(worst["metric"]) or 100.0)), 1)}
        else:
            ring = {"level": "stale" if gauges else "none", "metric": None, "value": None, "share": None,
                    "age_s": min((s["age_s"] for s in sources), default=None)}
        fc = self.forecasts(dev_id, now)
        lines: dict = {}
        cut = int((now - 86400) // SAMPLE_S)
        for g in gauges:
            pts = []
            for r in rows:
                if r["slot"] < cut or r["source"] != g["source"]:
                    continue
                v = r[g["metric"]]
                if v is None:
                    continue
                if g["metric"] == "load":
                    if not r["cores"]:
                        continue
                    v = round(100.0 * v / r["cores"], 1)
                pts.append([r["slot"] * SAMPLE_S, v])
            lines[g["metric"]] = pts
        return {"known": True, "t": round(now), "ring": ring, "gauges": gauges, "lines": lines, "step": SAMPLE_S,
                "sources": [{k: s[k] for k in ("source", "label", "age_s", "stale")} for s in sources],
                "forecasts": [{k: f.get(k) for k in ("metric", "level", "shape", "eta_h", "slope_h", "limit", "current", "source", "window",
                                                     "n", "points", "spans")} for f in fc],
                "limits": LIMITS, "levels": {m: LEVELS.get(m, DEFAULT_LEVELS) for m in METRICS}}

    # ------------------------------------------------------------------ background
    def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        try:
            if self.maybe_sample(now):
                self.announce(now)
        except Exception:
            log.exception("host health sample failed")

    async def loop(self) -> None:
        last_prune = 0.0
        while True:
            try:
                await asyncio.to_thread(self.tick)
                now = self.clock()
                if now - last_prune >= 6 * 3600:
                    last_prune = now
                    await asyncio.to_thread(self.prune, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("host health loop failed")
            await asyncio.sleep(30)


# ---------------------------------------------------------------------- "waiting on someone": batteries to replace
def onsite_note(core, now: float) -> list:
    """Overview row per hub with batteries that need replacing now: only a person can end it (``human``), so after
    24 hours unchanged it moves to the "Waiting on someone" line instead of tinting the whole page."""
    from . import hubmerge
    out = []
    for integ_id, entry in sorted((getattr(core, "hub", None) or {}).items()):
        snap = entry.get("snapshot")
        if not snap:
            continue
        try:
            site = hubmerge.onsite(snap, entry.get("states") or {}, core.hub_radio(integ_id, now), now)
        except Exception:
            continue
        now_items = [i for i in site["items"] if i["cat"] == "now"]
        if not now_items:
            continue
        out.append({"id": "onsite:%s" % integ_id, "level": "info", "key": "ov.note.onsite", "sub": "ov.note.onsite.sub", "human": True,
                    "vars": {"n": len(now_items), "name": now_items[0]["name"]}, "link": "#/sensors/%s" % integ_id})
    return out


# ---------------------------------------------------------------------- nightly report and notification event
def _register() -> None:
    from .nightly.contrib import Contribution, item, register, section
    from .notify import events as ev
    from .overview import register_note

    def available(ctx) -> bool:
        hh = getattr(ctx.core, "hosthealth", None)
        return hh is not None and bool(hh.store.one("SELECT 1 AS x FROM host_samples LIMIT 1"))

    def build(ctx) -> dict:
        hh = ctx.core.hosthealth
        hints = hh.hints(ctx.until)
        items = [item("hh:%s:%s" % (h["device"], h["metric"]), "hh.report.item_temp" if h["metric"] == "temp" else "hh.report.item", "danger" if (h["eta_h"] or 0) < 24 else "warn", point=True,
                      name=h["title"], metric_key="hh.metric." + h["metric"], eta_dur=round((h["eta_h"] or 0) * 3600),
                      now=h["current"], limit=h["limit"]) for h in hints]
        if not items:
            return section("forecast", "hh.report.title", "hh.report.summary_calm", [])
        return section("forecast", "hh.report.title", "hh.report.summary", items, n=len(items))

    register(Contribution("forecast", 26, "hh.report.title", available, build))
    register_note(onsite_note)
    ev.register(ev.make("host_forecast", "hosts", 63, "warning",
                        available=lambda core: getattr(core, "hosthealth", None) is not None))


_register()

