"""Resilience (S13): the risk lens, the weak spots, emergency scenarios and "What the network rests on" - computed from what
Lanloom already knows. Nothing is switched, nothing is sent anywhere, no reading is invented: an axis without a source says
"unknown" and counts as half weak, never as good.

Sources (all already in the core):
  * the failure chain (lanloom/failure.py): the typed dependency graph and its breadth-first search;
  * own fields per component (``resilience_parts`` in the settings): power source (ups, mains, battery, poe, unknown) and spare
    (live, partial, cold, none) with a short note - an operator writes them on the component card;
  * the control room (lanloom/controlroom): the battery backups with their live runtime, the internet watch with the reserve
    line, the backup chains with "restore last checked";
  * availability (5-minute slots), host health warnings, vulnerability hints, end-of-updates hints of the inventory and the
    certificates of the services.

Seven axes per component, each a weakness 0 ... 1 (0 = good, None = unknown) with its reasons (i18n keys with variables):
  impact     weighted number of parts that fail with it (sure): device, infrastructure, container, connection, hub device,
             automation 1, script 0.5, entity 0.05; a = log(1 + n) / log(1 + IMPACT_FULL), at most 1.
  spare      own field: live 0, cold 0.35, partial 0.5, none 1; the internet with a reserve line 0; nothing depends on it 0;
             no entry: unknown.
  backup     backup chains that cover it (the devices a chain names, or the host of an agent timer; directly or through its host): ok with a restore checked in the last 90 days 0,
             ok 0.25, running 0.25, late 0.5, unknown 0.5, failed 0.9; a part that keeps data (storage, server, hub, broker,
             container, Lanloom itself, a host with guests or containers) without a chain 1; network gear without a chain: unknown; everything else does not count.
  power      own field (or derived): ups 0.1 (0.35 when the UPS asks for a new battery), battery 0.15, mains 1, unknown None.
             PoE follows the switch it hangs on, a guest, container or connection follows its host. A device a battery backup
             reports through is taken as "on the UPS" (derived, the card says so and the field overrides it).
  currency   open vulnerability hint (exploited 1, critical/high 0.8, medium 0.5, low 0.2), updates ended 0.8 (soon 0.3),
             certificate expired/red 1, yellow 0.6.
  watch      two axes: reachability (Lanloom checks presence by address) and a second, fresh source (a connection, a host
             agent, the hub or an adapter reports from it): both 0, one 0.5, none 1.
  health     availability of the last 35 days (>= 99.9 % 0, >= 99 % 0.2, >= 97 % 0.5, >= 90 % 0.8, below 1), +0.15 from three
             outages in seven days, an early warning of host health at least 0.6; no measurement: unknown.
weakness = weighted mean of the six axes after impact (WEIGHT; unknown counts as UNKNOWN); risk = 100 * weakness * (0.3 + 0.7 * a).
Levels: < 15 low, < 30 moderate, < 45 elevated, < 60 high, else critical. Hub devices are only rated by their impact
(risk = 60 * a) so the lens shows the hubs and bridges everything hangs on without inventing axes.

The score of the whole network (0 ... 100, higher is better) and a letter (A ... F): mix = 0.6 * impact-weighted mean of the
risks + 0.4 * mean of the three highest; score = 100 - mix. One value per day (the last of the day) is kept for 90 days.

Scenarios (simulation only: several roots at once in the failure chain):
  power      roots = mains at once, UPS parts when the battery runs out (live runtime of the UPS, minus the reserve an
             administrator keeps for a clean shutdown; without a live value the runtime from the settings, marked as such).
             Any minute 0 ... 240 (the slider). Unknown power source: an own list "open", never guessed.
  internet   the internet node and everything set to depend on it; the reserve line (when the control room knows one) and the
             automations that keep running locally are named.
  node       the gateway, storage boxes, servers, the hub host and the parts with the highest impact, one at a time.

Pure computation without a server: ``compute(inputs)``; the core feeds it with ``Resilience(core).inputs()``."""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime
from typing import Optional

AXES = ("impact", "spare", "backup", "power", "currency", "watch", "health")
WEIGHT = {"spare": 0.22, "backup": 0.20, "power": 0.16, "currency": 0.12, "watch": 0.14, "health": 0.16}
UNKNOWN = 0.5
KIND_WEIGHT = {"device": 1.0, "node": 1.0, "container": 1.0, "connection": 1.0, "hub": 1.0, "hub_device": 1.0, "automation": 1.0,
               "script": 0.5, "entity": 0.05, "internet": 1.0}
IMPACT_FULL = 80
LEVELS = ("low", "moderate", "elevated", "high", "critical")
BOUNDS = (15, 30, 45, 60)
GRADES = ((85, "A"), (72, "B"), (60, "C"), (48, "D"), (36, "E"))
POWER = ("ups", "mains", "battery", "poe", "unknown")
SPARE = ("live", "partial", "cold", "none")
SPARE_WEAK = {"live": 0.0, "cold": 0.35, "partial": 0.5, "none": 1.0}
POWER_WEAK = {"ups": 0.1, "battery": 0.15, "mains": 1.0}
RATED_KINDS = ("device", "node", "container", "connection", "hub")
INFRA = ("router", "switch", "ap", "nas", "server", "hub", "broker", "self")
DATA_KINDS = ("nas", "server", "hub", "broker", "self")
NET_GEAR = ("router", "switch", "ap")
FRESH_SOURCES = ("controller", "adapter", "snmp", "agent", "hub", "lldp")      # a source that reports from the device itself
SEV_WEAK = {"critical": 0.8, "high": 0.8, "medium": 0.5, "low": 0.2}
SPARE_AFTER = 0.55          # a missing spare is only the recommendation for central parts (impact at least ...)
TOP = 10
NODE_SCENARIOS = 6
MINUTE_MAX = 240
TIMELINE = (0, 5, 15, 30, 60, 120)
HISTORY_DAYS = 90
RESTORE_OK_DAYS = 90
NOTE_MAX = 120
MAX_PARTS = 2000
PARTS = "resilience_parts"
SETTINGS = "resilience_settings"
HISTORY = "resilience_history"
DEFAULTS = {"runtime_min": None, "reserve_min": 0}
CACHE_S = 60.0
DEPTH = 8
EFFORT = ("small", "medium", "large")


def level_of(risk) -> Optional[str]:
    if risk is None:
        return None
    for b, lv in zip(BOUNDS, LEVELS):
        if risk < b:
            return lv
    return LEVELS[-1]


def impact_value(n_weighted: float) -> float:
    return min(1.0, math.log1p(max(0.0, n_weighted)) / math.log1p(IMPACT_FULL))


def risk_of(axes: dict, a: float) -> tuple:
    """axes = {name: {"weak": 0..1|None, "counts": bool}} -> (risk 0..100, weakness 0..1)."""
    z = n = 0.0
    for k, w in WEIGHT.items():
        ax = axes.get(k) or {}
        if ax.get("counts") is False:
            continue
        s = ax.get("weak")
        z += w * (UNKNOWN if s is None else s)
        n += w
    weak = z / n if n else 0.0
    return round(100.0 * weak * (0.3 + 0.7 * a), 1), weak


def score_of(risks: list) -> Optional[dict]:
    """[(risk, a)] -> {score, grade, mean, top} or None for an empty list."""
    if not risks:
        return None
    w = [(r, 0.3 + 0.7 * a) for r, a in risks]
    mean = sum(r * x for r, x in w) / sum(x for _, x in w)
    top = sorted((r for r, _ in risks), reverse=True)[:3]
    top_m = sum(top) / len(top)
    mix = 0.6 * mean + 0.4 * top_m
    score = int(round(max(0.0, min(100.0, 100.0 - mix))))
    return {"score": score, "grade": grade_of(score), "mean": round(mean, 1), "top": round(top_m, 1)}


def grade_of(score) -> Optional[str]:
    if score is None:
        return None
    for bound, g in GRADES:
        if score >= bound:
            return g
    return "F"


def R(key: str, **v) -> dict:
    """One reason: an i18n key below ``rs.why.`` with its variables."""
    return {"key": key, "vars": v}


# =========================================================================== own fields per component
def clean_part(data: dict) -> dict:
    """{power, spare, note} -> the cleaned entry; ValueError(i18n key) for a wrong value. Empty values are dropped."""
    out = {}
    p = data.get("power")
    if p not in (None, ""):
        if p not in POWER:
            raise ValueError("rs.err.power")
        out["power"] = p
    s = data.get("spare")
    if s not in (None, ""):
        if s not in SPARE:
            raise ValueError("rs.err.spare")
        out["spare"] = s
    note = data.get("note")
    if note not in (None, ""):
        if not isinstance(note, str):
            raise ValueError("rs.err.invalid")
        clean = "".join(ch for ch in note if ch.isprintable()).strip()[:NOTE_MAX]
        if clean:
            out["note"] = clean
    return out


def parts_of(store) -> dict:
    raw = store.get_setting(PARTS, {}) or {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(raw, dict) else {}


def set_part(store, node: str, data: dict, now: Optional[float] = None) -> Optional[dict]:
    parts = parts_of(store)
    entry = clean_part(data)
    if not entry:
        parts.pop(node, None)
    else:
        if node not in parts and len(parts) >= MAX_PARTS:
            raise ValueError("rs.err.too_many")
        entry["t"] = round(time.time() if now is None else now)
        parts[node] = entry
    store.set_setting(PARTS, parts)
    return parts.get(node)


def settings_of(store) -> dict:
    raw = store.get_setting(SETTINGS, {}) or {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        if isinstance(raw.get("runtime_min"), (int, float)) and 1 <= raw["runtime_min"] <= 600:
            out["runtime_min"] = int(raw["runtime_min"])
        if isinstance(raw.get("reserve_min"), (int, float)) and 0 <= raw["reserve_min"] <= 120:
            out["reserve_min"] = int(raw["reserve_min"])
    return out


def clean_settings(data: dict) -> dict:
    out = {}
    if "runtime_min" in data:
        v = data["runtime_min"]
        if v in (None, ""):
            out["runtime_min"] = None
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= 600:
            raise ValueError("rs.err.runtime")
        else:
            out["runtime_min"] = int(v)
    if "reserve_min" in data:
        v = data["reserve_min"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 120:
            raise ValueError("rs.err.reserve")
        out["reserve_min"] = int(v)
    if not out:
        raise ValueError("rs.err.invalid")
    return out


# =========================================================================== the axes (pure functions)
def axis_impact(counts: dict) -> dict:
    """counts = {"sure": {kind: n}, "likely": n} -> axis."""
    sure = counts.get("sure") or {}
    nw = sum(KIND_WEIGHT.get(k, 1.0) * v for k, v in sure.items())
    a = impact_value(nw)
    devices = sum(sure.get(k, 0) for k in ("device", "node", "container"))
    autos = sure.get("automation", 0) + sure.get("script", 0)
    hub = sure.get("hub_device", 0)
    entities = sure.get("entity", 0)
    total = sum(sure.values())
    links = total - devices - hub - autos - entities       # connections, hubs, the internet: the total always equals its breakdown
    why = []
    if not sure and not counts.get("likely"):
        why.append(R("impact_none"))
        value = "none"
    else:
        # the headline is about the devices; the other groups go on a smaller line. The browser builds that line from
        # fail.part.* and drops the zeros (a translation may not have an empty plural branch), so rs.why.impact_also is
        # only ever rendered in web/js/resilience.js: rendered anywhere else it would show {parts} literally.
        why.append(R("impact_devices", n=devices) if devices else R("impact_no_devices"))
        if hub or autos or entities or links:
            why.append(R("impact_also", hub_devices=hub, automations=autos, entities=entities, connections=links))
        if counts.get("likely"):
            why.append(R("impact_likely", n=counts["likely"]))
        value = "parts"
    return {"axis": "impact", "weak": round(a, 3), "value": value, "why": why, "n": round(nw, 1), "devices": devices, "autos": autos, "entities": entities, "links": links}


def axis_spare(field: Optional[str], has_dependants: bool, reserve_line: Optional[str] = None, note: str = "") -> dict:
    if reserve_line is not None:
        return {"axis": "spare", "weak": 0.0, "value": "live", "why": [R("spare_reserve", name=reserve_line)]}
    if field in SPARE_WEAK:
        why = [R("spare_" + field)]
        return {"axis": "spare", "weak": SPARE_WEAK[field], "value": field, "why": why}
    if not has_dependants:
        return {"axis": "spare", "weak": 0.0, "value": "not_needed", "why": [R("spare_not_needed")]}
    return {"axis": "spare", "weak": None, "value": "unknown", "why": [R("spare_unknown")]}


def cover_by_device(chains: list) -> dict:
    """Backup chain rows by the devices they cover: a chain may name several (``devices``), the older single ``device`` still counts.
    A chain that names none does not appear; the order of the chains is kept."""
    out: dict = {}
    for c in chains:
        for d in (c.get("devices") or ([c["device"]] if c.get("device") else [])):
            out.setdefault(d, []).append(c)
    return out


def axis_backup(cover: list, keeps_data: bool, net_gear: bool, now: float) -> dict:
    """cover = [{"name", "status", "restore_t", "via"}] - the backup chains that cover the part (directly or through its host)."""
    if not cover:
        if keeps_data:
            return {"axis": "backup", "weak": 1.0, "value": "none", "why": [R("backup_none")]}
        if net_gear:
            return {"axis": "backup", "weak": None, "value": "unknown", "why": [R("backup_config")]}
        return {"axis": "backup", "weak": None, "counts": False, "value": "not_rated", "why": [R("backup_not_rated")]}
    best, why, restored = None, [], False
    for c in cover:
        st = c.get("status") or "unknown"
        w = {"ok": 0.25, "running": 0.25, "late": 0.5, "unknown": 0.5, "none": 0.5, "failed": 0.9}.get(st, 0.5)
        fresh_restore = bool(c.get("restore_t") and now - c["restore_t"] <= RESTORE_OK_DAYS * 86400)
        if st in ("ok", "running") and fresh_restore:
            w = 0.0
            restored = True
        key = "backup_chain_via" if c.get("via") else "backup_chain"
        why.append(R(key, name=c.get("name") or "", status_key="rs.status." + (st if st in ("ok", "running", "late", "failed") else "unknown"),
                     via=c.get("via") or ""))
        if c.get("restore_t"):
            why.append(R("backup_restore", name=c.get("name") or "", at_at=c["restore_t"]))
        best = w if best is None else min(best, w)
    value = "checked" if best == 0.0 else "ok" if best <= 0.25 else "late" if best <= 0.5 else "failed"
    out = {"axis": "backup", "weak": best, "value": value, "why": why}
    if not restored and best is not None and best <= 0.25:
        out["why"].append(R("backup_no_restore"))
    return out


def axis_power(power: Optional[str], source: str, via: str = "", ups: Optional[dict] = None, note: str = "") -> dict:
    """power = ups|mains|battery|unknown|None, source = own|derived|via|none."""
    if power in (None, "unknown"):
        return {"axis": "power", "weak": None, "value": "unknown", "why": [R("power_unknown")], "source": source}
    why = [R("power_" + power + ("_via" if source == "via" else ""), via=via)]
    if source == "derived":
        why = [R("power_derived", name=(ups or {}).get("name") or "")]
    w = POWER_WEAK.get(power, 0.5)
    if power == "ups" and ups and ups.get("state") == "replace":
        w = 0.35
        why.append(R("power_replace", name=ups.get("name") or ""))
    return {"axis": "power", "weak": w, "value": power, "why": why, "source": source}


def axis_currency(vulns: list, eol: Optional[dict], certs: list) -> dict:
    w, why, value = 0.0, [], "current"
    for f in vulns or []:
        s = 1.0 if f.get("exploited") else SEV_WEAK.get(f.get("severity"), 0.5)
        if s > w:
            w = s
        value = "vulnerable"
        sev = f.get("severity") if f.get("severity") in SEV_WEAK else "medium"
        why.append(R("currency_vuln_exploited" if f.get("exploited") else "currency_vuln", id=f.get("id") or "", severity_key="rs.severity." + sev))
    if len(why) > 3:
        more = len(why) - 3
        why = why[:3] + [R("currency_more", n=more)]
    if eol and eol.get("state") in ("past", "soon"):
        s = 0.8 if eol["state"] == "past" else 0.3
        w = max(w, s)
        value = value if value != "current" else ("eol" if eol["state"] == "past" else "eol_soon")
        why.append(R("currency_eol_" + eol["state"], until=eol.get("until") or ""))
    for c in certs or []:
        s = 1.0 if c.get("tier") in ("danger", "expired") else 0.6 if c.get("tier") == "warn" else 0.0
        if s:
            w = max(w, s)
            value = value if value != "current" else "certificate"
            why.append(R("currency_cert", name=c.get("name") or "", days=c.get("days") if c.get("days") is not None else 0))
    if not why:
        why = [R("currency_ok")]
    return {"axis": "currency", "weak": w, "value": value, "why": why}


def axis_watch(reach: bool, fresh: Optional[str]) -> dict:
    why = [R("watch_reach" if reach else "watch_no_reach"), R("watch_fresh", source_key="rs.source." + fresh) if fresh else R("watch_no_fresh")]
    if reach and fresh:
        return {"axis": "watch", "weak": 0.0, "value": "two", "why": why}
    if reach or fresh:
        return {"axis": "watch", "weak": 0.5, "value": "reach" if reach else "fresh", "why": why}
    return {"axis": "watch", "weak": 1.0, "value": "none", "why": why}


def axis_health(stab: Optional[dict], warnings: list) -> dict:
    why, w, value = [], None, "unknown"
    share = None
    if stab:
        share = stab.get("share35") if stab.get("share35") is not None else stab.get("share7")
    if share is not None:
        pct = 100.0 * share
        w = 0.0 if pct >= 99.9 else 0.2 if pct >= 99 else 0.5 if pct >= 97 else 0.8 if pct >= 90 else 1.0
        inc = int(stab.get("incidents7") or 0)
        if inc >= 3:
            w = min(1.0, w + 0.15)
        why.append(R("health_avail", pct=round(pct, 2), n=inc))
        value = "measured"
    else:
        why.append(R("health_none"))
    for h in warnings or []:
        w = max(w if w is not None else 0.0, 0.6)
        eta = h.get("eta_h")
        why.append(R("health_warning_eta" if isinstance(eta, (int, float)) else "health_warning", metric=str(h.get("metric") or ""),
                     eta_dur=int(eta * 3600) if isinstance(eta, (int, float)) else 0))
        value = "warning"
    return {"axis": "health", "weak": w, "value": value, "why": why}


REC_AXIS = {
    "spare": ("spare", "large"), "spare_device": ("spare_device", "medium"), "spare_unknown": ("spare_unknown", "small"),
    "backup": ("backup", "medium"), "backup_restore": ("backup_restore", "medium"), "backup_late": ("backup_late", "small"),
    "backup_config": ("backup_config", "small"),
    "power": ("power", "small"), "power_unknown": ("power_unknown", "small"), "power_replace": ("power_replace", "small"),
    "currency_vuln": ("currency_vuln", "small"), "currency_eol": ("currency_eol", "large"), "currency_cert": ("currency_cert", "small"),
    "watch": ("watch", "small"), "health": ("health", "medium"), "health_warning": ("health_warning", "small"),
}


def recommendation(axes: dict, infra: bool) -> Optional[dict]:
    """The axis with the largest share in the risk -> {key, effort, axis}. A missing spare is only named for central parts."""
    a = (axes.get("impact") or {}).get("weak") or 0.0
    rows = []
    for k, w in WEIGHT.items():
        ax = axes.get(k) or {}
        if ax.get("counts") is False:
            continue
        if k == "spare" and a < SPARE_AFTER:
            continue
        s = ax.get("weak")
        rows.append((w * (UNKNOWN if s is None else s), k))
    rows.sort(reverse=True)
    if not rows or rows[0][0] <= 0:
        return None
    k = rows[0][1]
    ax = axes[k]
    key = k
    if k == "spare":
        key = "spare_unknown" if ax.get("weak") is None else ("spare" if infra else "spare_device")
    elif k == "backup":
        key = ("backup_config" if ax.get("weak") is None else "backup" if ax.get("value") == "none"
               else "backup_late" if (ax.get("weak") or 0) >= 0.5 else "backup_restore")
    elif k == "power":
        key = "power_unknown" if ax.get("weak") is None else "power_replace" if ax.get("value") == "ups" else "power"
    elif k == "currency":
        key = {"vulnerable": "currency_vuln", "eol": "currency_eol", "eol_soon": "currency_eol", "certificate": "currency_cert"}.get(ax.get("value"), "currency_vuln")
    elif k == "health":
        key = "health_warning" if ax.get("value") == "warning" else "health"
    rk, effort = REC_AXIS[key]
    return {"key": rk, "effort": effort, "axis": k}


# =========================================================================== the calculator
class Resilience:
    """Reads the core, keeps the last result for a minute (the graph or the own fields changing start a new one)."""

    def __init__(self, core, clock=time.time):
        self.core = core
        self.clock = clock
        self.lock = threading.Lock()
        self._res = None             # (key, t, result)

    # ----------------------------------------------------------------- inputs
    def _key(self, g) -> tuple:
        store = self.core.store
        return (id(g), repr(parts_of(store)), repr(settings_of(store)))

    def invalidate(self) -> None:
        with self.lock:
            self._res = None

    def result(self, now: Optional[float] = None, fresh: bool = False) -> dict:
        now = self.clock() if now is None else now
        fl = self.core.failure
        g = fl.graph()
        key = self._key(g)
        with self.lock:
            hit = self._res
            if not fresh and hit and hit[0] == key and 0 <= now - hit[1] < CACHE_S:
                return hit[2]
        res = self._compute(g, now)
        with self.lock:
            self._res = (key, now, res)
        if abs(now - self.clock()) < 600:          # a report built for a past night must not write that day's history
            self._remember(res, now)
        return res

    # ----------------------------------------------------------------- sources
    def _ups_rows(self, now: float) -> list:
        cr = getattr(self.core, "controlroom", None)
        if cr is None:
            return []
        try:
            return [u for u in cr.ups.view(now) if u.get("fresh") != "stale"]
        except Exception:
            return []

    def _wan(self, now: float) -> dict:
        cr = getattr(self.core, "controlroom", None)
        if cr is None:
            return {}
        try:
            v = cr.wan.view(now)
        except Exception:
            return {}
        reserve = [ln for ln in v.get("lines") or [] if ln.get("role") == "backup"]
        return {"state": v.get("state"), "reserve": (reserve[0].get("name") or reserve[0].get("id") or "") if reserve else None,
                "on_reserve": bool((v.get("failover") or {}).get("on_reserve"))}

    def _chains(self, now: float) -> list:
        cr = getattr(self.core, "controlroom", None)
        if cr is None:
            return []
        try:
            return cr.backups.chains(now)
        except Exception:
            return []

    def _stability(self, devices: list, now: float) -> dict:
        av = getattr(self.core, "availability", None)
        if av is None:
            return {}
        names = {"dev:" + d["id"]: {"title": d.get("title") or d["id"], "kind": d.get("kind") or ""} for d in devices}
        try:
            rows = av.stability(names, now)["rows"]
        except Exception:
            return {}
        return {r["id"][4:]: r for r in rows if str(r.get("id", "")).startswith("dev:")}

    def _vulns(self, now: float) -> dict:
        v = getattr(self.core, "vulns", None)
        if v is None:
            return {}
        try:
            res = v._all(now)
        except Exception:
            return {}
        out = {}
        for did, d in (res.get("devices") or {}).items():
            open_ = [f for f in d.get("findings") or [] if not f.get("dismissed")]
            if open_:
                out[did] = open_
        return out

    def _eol(self, now: float) -> dict:
        inv = getattr(self.core, "inventory", None)
        if inv is None:
            return {}
        try:
            items = inv.list({}, now=now)["items"]
        except Exception:
            return {}
        return {it["id"]: it["eol"] for it in items if it.get("eol") and it["eol"].get("state") in ("past", "soon")}

    def _certs(self) -> dict:
        sv = getattr(self.core, "services", None)
        if sv is None:
            return {}
        out = {}
        try:
            for r in sv.listing():
                c = r.get("cert")
                if c and c.get("tier") in ("warn", "danger", "expired") and r.get("device"):
                    out.setdefault(r["device"], []).append({"name": "%s:%s" % (r.get("title") or r.get("ip") or "", r.get("port")),
                                                            "tier": c["tier"], "days": c.get("days")})
        except Exception:
            return {}
        return out

    def _warnings(self, now: float) -> dict:
        hh = getattr(self.core, "hosthealth", None)
        if hh is None:
            return {}
        out = {}
        try:
            for w in hh.hints(now):
                out.setdefault(w["device"], []).append(w)
        except Exception:
            return {}
        return out

    def _agent_hosts(self) -> set:
        ag = getattr(self.core, "agents", None)
        if ag is None:
            return set()
        try:
            hosts = set(ag.containers_by_device().keys())
            for aid in list(getattr(ag, "agents", {}) or {}):
                dev = ag.device_of(aid)
                if dev:
                    hosts.add(dev)
            return hosts
        except Exception:
            return set()

    # ----------------------------------------------------------------- graph helpers
    @staticmethod
    def _parent(g, nid: str, whys: tuple) -> Optional[str]:
        for i in g.up.get(nid, ()):
            a, b, why, cert, _v = g.edges[i]
            if why in whys:
                return b
        return None

    def _counts(self, g, nid: str, memo: dict) -> dict:
        c = memo.get(nid)
        if c is None:
            found = self.core.failure.search(g, [nid], "down", DEPTH)
            sure, likely = {}, 0
            for n, rec in found.items():
                if rec[1] == "sure":
                    k = g.nodes[n]["kind"]
                    sure[k] = sure.get(k, 0) + 1
                else:
                    likely += 1
            c = memo[nid] = {"sure": sure, "likely": likely}
        return c

    # ----------------------------------------------------------------- the whole computation
    def _compute(self, g, now: float) -> dict:
        t0 = time.perf_counter()
        core, fl = self.core, self.core.failure
        store = core.store
        parts = parts_of(store)
        cfg = settings_of(store)
        devices = [d for d in core.devices() if not d.get("merged_into")]
        dev_by = {d["id"]: d for d in devices}
        ups_rows = self._ups_rows(now)
        wan = self._wan(now)
        chains = self._chains(now)
        stab = self._stability(devices, now)
        vulns, eol, certs, warns = self._vulns(now), self._eol(now), self._certs(), self._warnings(now)
        agent_hosts = self._agent_hosts()
        ups_dev = {u["device"]: u for u in ups_rows if u.get("device")}
        connected = {}                 # device -> name of a connection that reads from it
        for nid, info in g.nodes.items():
            if info["kind"] in ("connection", "hub"):
                dev = self._parent(g, nid, ("connection",))
                if dev:
                    connected.setdefault(dev, info.get("title") or "")
        with_device = {n for n in g.nodes if g.nodes[n]["kind"] in ("connection", "hub") and self._parent(g, n, ("connection",))}
        merged_hub = set()
        for a, b, why, cert, _v in g.edges:
            if why == "same":
                merged_hub.add(b)
        memo = {}

        # power, resolved once per node (PoE and guests follow their host)
        power_memo = {}

        def power_of(nid, depth=0):
            if nid in power_memo:
                return power_memo[nid]
            power_memo[nid] = (None, "none", "", None)          # loop guard
            info = g.nodes.get(nid) or {}
            own = (parts.get(nid) or {}).get("power")
            res = None
            if own and own not in ("poe",):
                res = (own if own != "unknown" else None, "own", "", None)
            elif depth < 6:
                whys = {"container": ("container",), "connection": ("connection",), "hub": ("connection",)}.get(info.get("kind"))
                if own == "poe":
                    whys = ("port",)
                parent = self._parent(g, nid, whys) if whys else self._parent(g, nid, ("virtual",))
                if parent:
                    p = power_of(parent, depth + 1)
                    res = (p[0], "via", fl.name(g, parent), p[3])
            if res is None and nid in ups_dev:
                res = ("ups", "derived", "", ups_dev[nid])
            if res is None:
                res = (None, "none", "", None)
            if res[0] == "ups" and res[3] is None and ups_rows:
                res = (res[0], res[1], res[2], ups_rows[0])
            power_memo[nid] = res
            return res

        # backup cover: direct, and through the host for guests and containers
        by_dev = cover_by_device(chains)

        def cover_of(nid):
            out = [{"name": c["name"], "status": c["status"], "restore_t": c.get("restore_checked_t")} for c in by_dev.get(nid, [])]
            host = self._parent(g, nid, ("container", "virtual"))
            if host:
                out += [{"name": c["name"], "status": c["status"], "restore_t": c.get("restore_checked_t"), "via": fl.name(g, host)}
                        for c in by_dev.get(host, [])]
            return out

        rated = {}
        for nid, info in g.nodes.items():
            kind = info["kind"]
            if kind not in RATED_KINDS or info.get("hidden"):
                continue
            dk = info.get("dev_kind") or ""
            if info.get("is_self"):
                dk = "self"
            z = self._counts(g, nid, memo)
            ax = {"impact": axis_impact(z)}
            has_dep = bool(z["sure"])
            part = parts.get(nid) or {}
            ax["spare"] = axis_spare(part.get("spare"), has_dep, None, part.get("note") or "")
            hosts_guests = any(g.edges[i][2] in ("container", "virtual") for i in g.down.get(nid, ()))
            keeps = dk in DATA_KINDS or kind == "container" or bool(by_dev.get(nid)) or hosts_guests      # a host keeps its guests' disks
            ax["backup"] = axis_backup(cover_of(nid), keeps, dk in NET_GEAR or kind == "node", now)
            pw = power_of(nid)
            ax["power"] = axis_power(pw[0], pw[1], pw[2], pw[3], part.get("note") or "")
            if kind == "device":
                ax["currency"] = axis_currency(vulns.get(nid), eol.get(nid), certs.get(nid))
                d = dev_by.get(nid) or {}
                srcs = [s for s in d.get("sources") or [] if s in FRESH_SOURCES and s != "lldp"]
                fresh = ("connection" if nid in connected else "agent" if nid in agent_hosts else "hub" if nid in merged_hub
                         else "adapter" if d.get("adapter") else (srcs[0] if srcs else None))
                prot = (d.get("protect") or {}).get("class")
                ax["watch"] = axis_watch(bool(d.get("ip")) and prot != "passive", fresh)
                ax["health"] = axis_health(stab.get(nid), warns.get(nid))
            elif kind == "node":
                ax["currency"] = axis_currency([], None, [])
                ax["watch"] = axis_watch(bool(info.get("ip")), "topology")
                ax["health"] = axis_health(None, [])
            else:                                      # container, connection, hub: what Lanloom reads from them
                ax["currency"] = axis_currency([], None, [])
                ax["watch"] = axis_watch(kind != "container", "agent" if kind == "container" else "connection")
                ax["health"] = axis_health(None, [])
            a = ax["impact"]["weak"]
            risk, weak = risk_of(ax, a)
            infra = dk in INFRA or kind in ("node", "hub", "connection")
            ranked = has_dep or (kind == "device" and dk in INFRA) or kind == "node"
            if kind in ("connection", "hub") and nid in with_device:
                ranked = False                         # the device it talks to carries the risk (one row, one tile)
            rated[nid] = {"id": nid, "kind": kind, "dev_kind": dk or None, "title": fl.name(g, nid), "risk": risk, "level": level_of(risk),
                          "weak": round(weak, 3), "a": a, "axes": ax, "infra": infra, "ranked": ranked,
                          "rec": recommendation(ax, infra), "tiles": list(info.get("tiles") or []), "pw": pw}
        # the internet: a reserve line is its spare (only the impact and the spare count there)
        if "internet" in g.nodes:
            z = self._counts(g, "internet", memo)
            ax = {"impact": axis_impact(z), "spare": axis_spare(None, bool(z["sure"]), wan.get("reserve"))}
            for k in ("backup", "power", "currency", "watch", "health"):
                ax[k] = {"axis": k, "weak": None, "counts": False, "value": "not_rated", "why": []}
            if wan.get("reserve") is None:
                ax["spare"] = {"axis": "spare", "weak": 1.0 if z["sure"] else 0.0, "value": "none" if z["sure"] else "not_needed",
                               "why": [R("spare_no_reserve" if z["sure"] else "spare_not_needed")]}
            a = ax["impact"]["weak"]
            risk, weak = risk_of(ax, a)
            rated["internet"] = {"id": "internet", "kind": "internet", "dev_kind": None, "title": "", "risk": risk, "level": level_of(risk),
                                 "weak": round(weak, 3), "a": a, "axes": ax, "infra": True, "ranked": bool(z["sure"]),
                                 "rec": recommendation(ax, True) if z["sure"] else None, "tiles": ["internet"], "pw": (None, "none", "", None)}

        ranked = [r for r in rated.values() if r["ranked"]]
        ranked.sort(key=lambda r: (-r["risk"], r["title"].casefold()))
        score = score_of([(r["risk"], r["a"]) for r in ranked])
        if score:
            score["n"] = len(ranked)
            score["levels"] = {lv: sum(1 for r in ranked if r["level"] == lv) for lv in LEVELS}
        lens = self._lens(g, rated, memo)
        unknown_power = sorted((r["title"] for r in ranked if r["axes"]["power"]["weak"] is None and r["kind"] == "device"), key=str.casefold)
        scen = self._scenarios(g, rated, ranked, ups_rows, cfg, wan, now)
        layers = self._layers(g, rated, dev_by, ups_rows, wan)
        return {"t": now, "score": score, "weak_spots": [self._row(r) for r in ranked[:TOP]], "ranked_n": len(ranked),
                "axes": [{"id": k, "weight": WEIGHT.get(k)} for k in AXES],
                "levels": [{"id": lv, "below": (BOUNDS + (None,))[i]} for i, lv in enumerate(LEVELS)],
                "grades": [{"grade": gname, "from": b} for b, gname in GRADES] + [{"grade": "F", "from": 0}],
                "scenarios": scen, "layers": layers, "unknown_power": unknown_power[:20], "unknown_power_n": len(unknown_power),
                "settings": cfg, "ups": [{"id": u["id"], "name": u["name"], "state": u["state"], "runtime_s": u.get("runtime_s"),
                                          "charge": u.get("charge"), "load": u.get("load"), "device": u.get("device")} for u in ups_rows],
                "wan": wan, "_rated": rated, "_lens": lens, "graph": {"nodes": len(g.nodes), "edges": len(g.edges)},
                "ms": round((time.perf_counter() - t0) * 1000, 1)}

    @staticmethod
    def _row(r: dict) -> dict:
        return {"id": r["id"], "kind": r["kind"], "dev_kind": r["dev_kind"], "title": r["title"], "risk": r["risk"], "level": r["level"],
                "rec": r["rec"], "tiles": r["tiles"][:3], "impact": r["axes"]["impact"]["n"],
                "axes": {k: {"weak": r["axes"][k].get("weak"), "value": r["axes"][k].get("value"), "counts": r["axes"][k].get("counts", True)}
                         for k in AXES}}

    # ----------------------------------------------------------------- the lens: risk per tile
    def _lens(self, g, rated: dict, memo: dict) -> dict:
        tiles = {}

        def put(tile, risk, nid, only_impact=False):
            old = tiles.get(tile)
            if old is None or risk > old["r"]:
                tiles[tile] = {"r": risk, "l": level_of(risk), "id": nid, **({"impact": True} if only_impact else {})}
        for nid, r in rated.items():
            if not r["ranked"]:
                continue
            for x in r["tiles"][:1]:
                put(x, r["risk"], nid)
        for nid, info in g.nodes.items():
            if info["kind"] != "hub_device":
                continue
            z = self._counts(g, nid, memo)
            nw = sum(KIND_WEIGHT.get(k, 1.0) * v for k, v in z["sure"].items())
            if nw < 1:
                continue
            risk = round(60.0 * impact_value(nw), 1)
            for x in (info.get("tiles") or [])[:1]:
                put(x, risk, nid, True)
        return tiles

    # ----------------------------------------------------------------- scenarios
    def _sum(self, g, found: dict, roots: list) -> dict:
        z = {"devices": 0, "hub_devices": 0, "automations": 0, "entities": 0, "connections": 0, "sure": 0, "likely": 0}
        for nid in list(found) + list(roots):
            rec = found.get(nid)
            k = g.nodes[nid]["kind"]
            if rec is None or rec[1] == "sure":
                z["sure"] += 1
                key = {"device": "devices", "node": "devices", "container": "devices", "hub_device": "hub_devices", "automation": "automations",
                       "script": "automations", "entity": "entities", "connection": "connections", "hub": "connections"}.get(k)
                if key:
                    z[key] += 1
            else:
                z["likely"] += 1
        return z

    def _run(self, g, roots: list) -> tuple:
        roots = [r for r in roots if r in g.nodes]
        found = self.core.failure.search(g, roots, "down", DEPTH) if roots else {}
        return found, roots

    def _top_names(self, g, found: dict, rated: dict, n: int = 4, likely: bool = False) -> list:
        rows = [((rated.get(nid) or {}).get("a", 0.0), self.core.failure.name(g, nid)) for nid, rec in found.items()
                if (rec[1] == "sure" or likely) and g.nodes[nid]["kind"] in ("device", "node", "container", "connection", "hub")]
        out = []
        for _a, nm in sorted(rows, key=lambda x: (-x[0], x[1].casefold())):
            if nm not in out:
                out.append(nm)
            if len(out) >= n:
                break
        return out

    @staticmethod
    def ups_cutoff(ups: Optional[dict], cfg: dict) -> tuple:
        """-> (seconds until the UPS parts go off, basis live|setting|None)."""
        reserve = 60 * int(cfg.get("reserve_min") or 0)
        if ups and isinstance(ups.get("runtime_s"), (int, float)) and ups["runtime_s"] > 0:
            return max(0, int(ups["runtime_s"]) - reserve), "live"
        if cfg.get("runtime_min"):
            return max(0, 60 * int(cfg["runtime_min"]) - reserve), "setting"
        return None, None

    def _power_roots(self, rated: dict, minute: float, cfg: dict) -> dict:
        mains, ups_parts, open_, battery = [], [], [], []
        cut = {}
        for nid, r in rated.items():
            if r["kind"] not in ("device", "node"):
                continue
            p = r["pw"]
            if p[0] == "mains":
                mains.append(nid)
            elif p[0] == "ups":
                s, basis = self.ups_cutoff(p[3], cfg)
                cut[nid] = (s, basis, (p[3] or {}).get("name") or "")
                ups_parts.append(nid)
            elif p[0] == "battery":
                battery.append(nid)
            elif r["ranked"]:
                open_.append(nid)
        off_ups = [n for n in ups_parts if cut[n][0] is not None and minute * 60 >= cut[n][0]]
        return {"mains": mains, "ups": ups_parts, "off_ups": off_ups, "open": open_, "battery": battery, "cut": cut}

    def _scenarios(self, g, rated, ranked, ups_rows, cfg, wan, now) -> list:
        out = []
        fl = self.core.failure
        name = lambda nid: fl.name(g, nid)          # noqa: E731
        hub_nodes = [n for n, i in g.nodes.items() if i["kind"] == "hub"]
        n_auto = sum(1 for i in g.nodes.values() if i["kind"] in ("automation", "script"))
        # --- power
        pr = self._power_roots(rated, MINUTE_MAX, cfg)
        if pr["mains"] or pr["ups"] or pr["open"]:
            timeline = []
            for m in TIMELINE:
                roots = pr["mains"] + [n for n in pr["ups"] if pr["cut"][n][0] is not None and m * 60 >= pr["cut"][n][0]]
                found, rr = self._run(g, roots)
                timeline.append({"min": m, "counts": self._sum(g, found, rr)})
            cuts = sorted({v[0] for v in pr["cut"].values() if v[0] is not None})
            basis = sorted({v[1] for v in pr["cut"].values() if v[1]})
            ups_names = sorted({v[2] for v in pr["cut"].values() if v[2]})
            hub_off_at = None
            for m in [0] + [c / 60.0 for c in cuts]:
                roots = pr["mains"] + [n for n in pr["ups"] if pr["cut"][n][0] is not None and m * 60 >= pr["cut"][n][0]]
                found, rr = self._run(g, roots)
                if any(h in found and found[h][1] == "sure" for h in hub_nodes):
                    hub_off_at = round(m, 1)
                    break
            out.append({"id": "power", "kind": "power", "cut_s": cuts[0] if cuts else None, "cuts_s": cuts, "basis": basis[0] if basis else None,
                        "ups_names": ups_names, "max_min": MINUTE_MAX, "timeline": timeline, "hub_off_min": hub_off_at,
                        "reserve_min": cfg.get("reserve_min") or 0,
                        "groups": [grp for grp in (
                            {"id": "ups", "items": [{"id": n, "name": name(n), "off_s": pr["cut"][n][0]} for n in sorted(pr["ups"], key=lambda x: name(x).casefold())]},
                            {"id": "mains", "items": [{"id": n, "name": name(n)} for n in sorted(pr["mains"], key=lambda x: name(x).casefold())]},
                            {"id": "battery", "items": [{"id": n, "name": name(n)} for n in sorted(pr["battery"], key=lambda x: name(x).casefold())]},
                            {"id": "open", "items": [{"id": n, "name": name(n)} for n in sorted(pr["open"], key=lambda x: name(x).casefold())][:40]},
                        ) if grp["items"]],
                        "no_ups": not ups_rows})
        # --- internet
        if "internet" in g.nodes:
            found, rr = self._run(g, ["internet"])
            z = self._sum(g, found, [])
            autos_off = sum(1 for nid, rec in found.items() if g.nodes[nid]["kind"] in ("automation", "script") and rec[1] == "sure")
            hub_off = any(h in found and found[h][1] == "sure" for h in hub_nodes)
            out.append({"id": "internet", "kind": "internet", "counts": z, "reserve": wan.get("reserve"), "on_reserve": wan.get("on_reserve", False),
                        "autos": {"total": n_auto, "off": autos_off}, "hub_off": hub_off, "has_hub": bool(hub_nodes),
                        "names": self._top_names(g, found, rated, 4, likely=True), "manual_n": sum(1 for i in g.down.get("internet", ()) if g.edges[i][2].startswith("manual_"))})
        # --- single parts: gateway, storage, servers, the hub host, then the highest impact
        picks = []
        for r in sorted(rated.values(), key=lambda r: -r["a"]):
            if r["kind"] != "device" or not r["ranked"]:
                continue
            info = g.nodes[r["id"]]
            if info.get("gateway") or r["dev_kind"] in ("nas", "server", "hub", "self"):
                picks.append(r["id"])
        for r in sorted(rated.values(), key=lambda r: -r["a"]):
            if r["id"] not in picks and r["kind"] in ("device", "node") and r["a"] > 0:
                picks.append(r["id"])
        n_node = 0
        for nid in picks:
            if n_node >= NODE_SCENARIOS:
                break
            found, rr = self._run(g, [nid])
            z = self._sum(g, found, [])
            info = g.nodes[nid]
            if not z["sure"] and not info.get("gateway"):
                continue                               # nothing fails with it: not an emergency
            n_node += 1
            role = "gateway" if info.get("gateway") else (rated[nid]["dev_kind"] or rated[nid]["kind"])
            out.append({"id": "node:" + nid, "kind": "node", "node": nid, "name": name(nid), "role": role, "counts": z,
                        "hub_off": any(h in found and found[h][1] == "sure" for h in hub_nodes),
                        "names": [x for x in self._top_names(g, found, rated, 5) if x != name(nid)][:4], "spare": (rated[nid]["axes"]["spare"] or {}).get("value"),
                        "risk": rated[nid]["risk"], "level": rated[nid]["level"]})
        return out

    def scenario(self, sid: str, minute: Optional[float] = None, now: Optional[float] = None) -> Optional[dict]:
        """One scenario with the full list of affected parts in the format of the failure chain (the map shows it), or None."""
        t0 = time.perf_counter()
        res = self.result(now)
        sc = next((s for s in res["scenarios"] if s["id"] == sid), None)
        if sc is None:
            return None
        fl = self.core.failure
        g = fl.graph()
        rated = res["_rated"]
        title = {"key": "rs.sc." + sc["kind"] + ".title", "vars": {"name": sc.get("name") or ""}}
        extra = {}
        if sc["kind"] == "power":
            m = sc["max_min"] if minute is None else max(0.0, min(float(sc["max_min"]), float(minute)))
            cfg = settings_of(self.core.store)
            pr = self._power_roots(rated, m, cfg)
            roots = pr["mains"] + pr["off_ups"]
            extra = {"minute": round(m, 1), "running": [{"id": n, "name": fl.name(g, n), "off_s": pr["cut"][n][0]} for n in pr["ups"] if n not in pr["off_ups"]],
                     "open": [{"id": n, "name": fl.name(g, n)} for n in pr["open"]][:40]}
        elif sc["kind"] == "internet":
            roots = ["internet"]
        else:
            roots = [sc["node"]]
        found, rr = self._run(g, roots)
        rootset = set(rr)
        items, more = fl._list(g, found, rootset)
        counts, stages = fl.counts(g, found)
        roots_view = [fl.view(g, r) for r in rr]
        return dict({"scenario": {k: v for k, v in sc.items() if k not in ("timeline",)}, "title": title, "dir": "down",
                     "root": roots_view[0] if len(roots_view) == 1 else {"id": "scenario:" + sid, "kind": "scenario", "title": "", "tiles": []},
                     "roots": roots_view, "counts": counts, "stages": stages, "items": items, "more": more,
                     "ms": round((time.perf_counter() - t0) * 1000, 1)}, **extra)

    # ----------------------------------------------------------------- layers: what the network rests on
    def _layers(self, g, rated: dict, dev_by: dict, ups_rows: list, wan: dict) -> list:
        fl = self.core.failure
        layer_of = {}
        for nid, r in rated.items():
            info = g.nodes.get(nid) or {}
            k, dk = r["kind"], r["dev_kind"] or ""
            if k == "internet":
                layer_of[nid] = "internet"
            elif info.get("gateway"):
                layer_of[nid] = "gateway"
            elif k == "node" or dk in NET_GEAR:
                layer_of[nid] = "network"
            elif dk in ("nas", "server", "self") or (k == "device" and any(g.edges[i][2] in ("container", "virtual") for i in g.down.get(nid, ()))):
                layer_of[nid] = "servers"
            elif k in ("hub", "connection") or dk in ("hub", "broker"):
                layer_of[nid] = "platforms"
            elif r["ranked"] or k == "container":
                layer_of[nid] = "services"
        out = []
        if ups_rows:
            out.append({"id": "power", "members": [u["name"] for u in ups_rows][:40], "n": len(ups_rows), "risk": None, "level": None,
                        "breaks": None, "names": [], "ups": [{"name": u["name"], "runtime_s": u.get("runtime_s"), "state": u["state"]} for u in ups_rows]})
        for lid in ("internet", "gateway", "network", "servers", "platforms", "services"):
            members = [n for n, l in layer_of.items() if l == lid]
            if not members:
                continue
            found, rr = self._run(g, members)
            per = {}
            for nid, rec in found.items():
                if rec[1] != "sure":
                    continue
                l2 = layer_of.get(nid) or ("hub" if g.nodes[nid]["kind"] in ("hub_device", "entity", "automation", "script") else None)
                if l2:
                    per[l2] = per.get(l2, 0) + 1
            risks = [rated[m]["risk"] for m in members if rated[m]["ranked"]]
            worst = max(risks) if risks else None
            weakest = max((m for m in members if rated[m]["ranked"]), key=lambda m: rated[m]["risk"], default=None)
            row = {"id": lid, "n": len(members), "members": sorted({fl.name(g, m) for m in members if m != "internet"}, key=str.casefold)[:40],
                   "risk": worst, "level": level_of(worst), "breaks": per, "names": self._top_names(g, found, rated, 6),
                   "weakest": {"id": weakest, "name": fl.name(g, weakest) if weakest != "internet" else ""} if weakest else None}
            if lid == "internet":
                row["reserve"] = wan.get("reserve")
            out.append(row)
        return out

    # ----------------------------------------------------------------- the card of one part
    def part(self, ident: str, now: Optional[float] = None) -> Optional[dict]:
        fl = self.core.failure
        g = fl.graph()
        nid = fl.resolve(g, ident)
        if nid is None:
            return None
        res = self.result(now)
        r = res["_rated"].get(nid)
        own = parts_of(self.core.store).get(nid) or {}
        editable = g.nodes[nid]["kind"] in ("device", "node", "container", "connection", "hub")
        if r is None:
            return {"node": fl.view(g, nid), "rated": False, "fields": own, "editable": editable, "power": list(POWER), "spare": list(SPARE)}
        rank = next((i + 1 for i, w in enumerate(res["weak_spots"]) if w["id"] == nid), None)
        return {"node": fl.view(g, nid), "rated": True, "risk": r["risk"], "level": r["level"], "ranked": r["ranked"], "rank": rank,
                "axes": [dict(r["axes"][k], weight=WEIGHT.get(k)) for k in AXES], "rec": r["rec"], "fields": own,
                "editable": editable, "power": list(POWER), "spare": list(SPARE)}

    def set_part(self, ident: str, data: dict) -> dict:
        fl = self.core.failure
        g = fl.graph()
        nid = fl.resolve(g, ident)
        if nid is None:
            raise ValueError("rs.err.unknown")
        if g.nodes[nid]["kind"] not in ("device", "node", "container", "connection", "hub"):
            raise ValueError("rs.err.kind")
        entry = set_part(self.core.store, nid, data)
        self.invalidate()
        return {"ok": True, "node": nid, "fields": entry or {}}

    def set_settings(self, data: dict) -> dict:
        clean = clean_settings(data)
        cur = settings_of(self.core.store)
        cur.update(clean)
        self.core.store.set_setting(SETTINGS, cur)
        self.invalidate()
        return settings_of(self.core.store)

    # ----------------------------------------------------------------- the history (one value per day)
    def _remember(self, res: dict, now: float) -> None:
        sc = res.get("score")
        if not sc:
            return
        try:
            day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            hist = self.history()
            if hist and hist[-1][0] == day and hist[-1][1] == sc["score"]:
                return
            rows = [x for x in hist if x[0] != day] + [[day, sc["score"], sc["grade"]]]
            self.core.store.set_setting(HISTORY, sorted(rows)[-HISTORY_DAYS:])
        except Exception:
            pass

    def history(self) -> list:
        raw = self.core.store.get_setting(HISTORY, []) or []
        return [x for x in raw if isinstance(x, list) and len(x) >= 2 and isinstance(x[0], str) and isinstance(x[1], (int, float))]

    def public(self, now: Optional[float] = None) -> dict:
        res = self.result(now)
        out = {k: v for k, v in res.items() if not k.startswith("_")}
        out["history"] = self.history()
        return out

    def lens(self, tiles: Optional[set] = None, now: Optional[float] = None) -> dict:
        res = self.result(now)
        lens = self._lens_of(res)
        if tiles is not None:
            lens = {k: v for k, v in lens.items() if k in tiles}
        return {"tiles": lens, "score": res.get("score"), "levels": [lv for lv in LEVELS]}

    def _lens_of(self, res: dict) -> dict:
        cached = res.get("_lens")
        if cached is None:
            fl = self.core.failure
            g = fl.graph()
            cached = res["_lens"] = self._lens(g, res["_rated"], {})
        return cached
