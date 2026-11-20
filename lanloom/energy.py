"""Energy flow from the power sensors of a home-automation hub (S17): who draws how much right now, what it costs, how much is solar.

Only with hub energy: without a single power sensor in the hub snapshot the book is *not available* and nothing is shown. Everything
is read from what the hub bridge already holds (``controlroom.hubfacts``); nothing is asked from a device, nothing leaves the box.

Roles are guessed from the folded name of a power sensor (grid, export, solar, battery; everything else is a consumer lane). Signs:
a grid sensor may be negative (= feeding into the grid); a battery sensor is positive when it discharges into the home and negative
while it charges. The flows are derived so that they add up: solar covers the home first, the surplus charges the battery, the rest
goes to the grid; a missing part is taken from the battery and then from the grid.

Price per kWh and CO2 intensity come from a hub entity (unit ``EUR/kWh``, ``gCO2eq/kWh`` ...) or from a number a person typed in; a
named entity beats a typed number, a typed number beats an entity found by itself. Today's totals come from a "today" counter of the
hub when there is one, otherwise ``step()`` counts watts over time (trapezoid, gaps over 10 minutes are skipped) per local day and
keeps the last 35 days in the site's own store.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from . import hubmerge
from .controlroom import hubfacts, levels

log = logging.getLogger("lanloom")

SETTING = "energy"
DAYS_SETTING = "energy_days"
KEEP_DAYS = 35
MAX_LANES = 24
GAP_S = 600.0                      # a gap between two samples longer than this is not counted
SAVE_EVERY_S = 300.0
DEFAULT_CO2 = 400.0                # g/kWh when nothing is configured
MIN_FLOW_W = 1.0
POWER_UNITS = {"w": 1.0, "kw": 1000.0, "mw": 1e6}
ENERGY_UNITS = {"kwh": 1.0, "wh": 0.001, "mwh": 1000.0}
CURRENCIES = {"eur": "EUR", "€": "EUR", "usd": "USD", "$": "USD", "gbp": "GBP", "£": "GBP", "chf": "CHF"}
PRICE_RE = re.compile(r"\A(eur|€|usd|\$|gbp|£|chf|ct|cent|c)\s*/\s*kwh\Z")
CO2_RE = re.compile(r"co2.*?/\s*(k?wh)")
DEFAULTS = {"price": None, "currency": "EUR", "feed_in": None, "co2_g_kwh": None, "price_entity": None, "co2_entity": None}
TODAY_WORDS = ("today", "daily", "heute", "day", "tag", "taglich")
ROLE_WORDS = {
    "export": (("export", "return", "einspeis"), ("feed in", "feedin", "einspeis")),
    "battery": (("battery", "akku", "storage", "batterie"), ()),
    "solar": (("solar", "pv", "photovoltaic", "inverter"), ("photovoltaic",)),
    "grid": (("grid", "mains", "utility", "netz", "import"), ()),
    "home": ((), ("house consumption", "home consumption", "total consumption", "house power", "home power", "house total",
                  "gesamtverbrauch", "hausverbrauch", "gesamt verbrauch", "haus verbrauch")),
}
ROLE_ORDER = ("home", "export", "battery", "solar", "grid")


def _has(text: str, words: tuple, phrases: tuple) -> bool:
    tokens = set(text.split())
    if any(w in tokens or (len(w) > 5 and w in text) for w in words):
        return True
    return any(p in text for p in phrases)


def role_of(text: str) -> Optional[str]:
    for role in ROLE_ORDER:
        words, phrases = ROLE_WORDS[role]
        if _has(text, words, phrases):
            return role
    return None


def power_factor(e: dict) -> Optional[float]:
    f = POWER_UNITS.get(e["unit"].lower())
    if f is not None:
        return f
    return 1.0 if e["dc"] == "power" and not e["unit"] else None


def price_unit(unit: str):
    """(per-kWh factor, currency or None) for a price unit such as ``EUR/kWh``, ``ct/kWh``; None when it is no price."""
    u = unit.strip().lower()
    m = PRICE_RE.match(u)
    if not m:
        return None
    sym = m.group(1)
    if sym in ("ct", "cent", "c"):
        return 0.01, None
    return 1.0, CURRENCIES.get(sym)


def co2_factor(e: dict) -> Optional[float]:
    u = e["unit"].lower().replace(" ", "")
    if "co2" in u and "/kwh" in u:
        return 1000.0 if u.startswith("kg") else 1.0
    if u in ("g/kwh", "gco2/kwh") and "co2" in e["text"]:
        return 1.0
    return None


def num(v) -> Optional[float]:
    return hubfacts.as_number(v)


class EnergyBook:
    def __init__(self, core, clock=time.time):
        self.core = core
        self.clock = clock
        self._lock = threading.Lock()
        self._days: Optional[dict] = None
        self._last = None                   # (t, home_w, solar_w, export_w, import_w, price, feed_in, g)
        self._saved_t = 0.0
        self._saved_day = None

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        raw = self.core.store.get_setting(SETTING, None)
        out = dict(DEFAULTS)
        if isinstance(raw, dict):
            for k in DEFAULTS:
                if k in raw:
                    out[k] = raw[k]
        return out

    def set_settings(self, patch) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("energy.err.body")
        cur = self.settings()
        cl = self.classify()

        def number(key, lo, hi, err):
            v = patch[key]
            if v is None or v == "":
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                raise ValueError(err)
            x = num(v)
            if x is None or not lo <= x <= hi:
                raise ValueError(err)
            return round(x, 6)
        if "price" in patch:
            cur["price"] = number("price", 0, 10, "energy.err.price")
        if "feed_in" in patch:
            cur["feed_in"] = number("feed_in", 0, 10, "energy.err.feed_in")
        if "co2_g_kwh" in patch:
            cur["co2_g_kwh"] = number("co2_g_kwh", 0, 2000, "energy.err.co2")
        if "currency" in patch:
            c = patch["currency"]
            if not isinstance(c, str) or not re.fullmatch(r"[A-Z]{3}", c):
                raise ValueError("energy.err.currency")
            cur["currency"] = c
        for key, pool in (("price_entity", cl["price"]), ("co2_entity", cl["co2"])):
            if key in patch:
                v = patch[key]
                if v in (None, ""):
                    cur[key] = None
                elif isinstance(v, str) and v in pool:
                    cur[key] = v
                else:
                    raise ValueError("energy.err.entity")
        self.core.store.set_setting(SETTING, cur)
        return cur

    # ------------------------------------------------------------------ classification
    def classify(self) -> dict:
        """{"power": {role|None: [row]}, "price": {entity_id: row}, "co2": {entity_id: row}, "today": {...}} from the hub entities."""
        power = {r: [] for r in ROLE_ORDER}
        power[None] = []
        price, co2, counters = {}, {}, []
        for e in hubfacts.entities(self.core):
            if e["domain"] not in ("sensor", "number", "input_number"):
                continue
            v = num(e["state"])
            f = co2_factor(e)
            pf = price_unit(e["unit"])
            if f is not None:
                co2[e["entity_id"]] = dict(e, value=None if v is None else v * f)
                continue
            if pf is not None:
                cur = pf[1]
                price[e["entity_id"]] = dict(e, value=None if v is None else v * pf[0], currency=cur)
                continue
            pw = power_factor(e)
            if pw is not None:
                row = dict(e, value=None if v is None else v * pw, role=role_of(e["text"]))
                power[row["role"]].append(row)
                continue
            ef = ENERGY_UNITS.get(e["unit"].lower())
            if ef is None and e["dc"] == "energy" and not e["unit"]:
                ef = 1.0
            if ef is not None and v is not None and any(w in e["text"].split() for w in TODAY_WORDS):
                counters.append(dict(e, value=v * ef, role=role_of(e["text"])))
        return {"power": power, "price": price, "co2": co2, "counters": counters}

    @staticmethod
    def _sum(rows: list):
        """Sum of the readings of one role; a sensor named "total" wins over its parts. None when nothing reads."""
        rows = [r for r in rows if r["value"] is not None]
        if not rows:
            return None, []
        tot = [r for r in rows if "total" in r["text"].split() or "gesamt" in r["text"].split()]
        use = tot[:1] or rows
        return sum(r["value"] for r in use), use

    # ------------------------------------------------------------------ the picture
    def _pick(self, pool: dict, wanted: Optional[str], manual: Optional[float]):
        """(row, source): a named entity beats a typed number, a typed number beats an entity found by itself."""
        named = pool.get(wanted) if wanted else None
        if named is not None and named["value"] is not None:
            return named, "entity"
        if manual is not None:
            return None, "setting"
        auto = sorted((r for r in pool.values() if r["value"] is not None), key=lambda r: r["entity_id"])
        return (auto[0], "entity") if auto else (None, None)

    def _rates(self, cl: dict, st: dict):
        prow, psrc = self._pick(cl["price"], st["price_entity"], st["price"])
        price = None
        if psrc == "setting":
            price = {"value": st["price"], "currency": st["currency"], "source": "setting"}
        elif prow is not None:
            cur = price_unit(prow["unit"])[1] or st["currency"]
            price = {"value": round(prow["value"], 6), "currency": cur, "source": "entity"}
        crow, csrc = self._pick(cl["co2"], st["co2_entity"], st["co2_g_kwh"])
        if csrc == "setting":
            co2 = {"g_kwh": st["co2_g_kwh"], "source": "setting"}
        elif crow is not None:
            co2 = {"g_kwh": round(crow["value"], 1), "source": "entity"}
        else:
            co2 = {"g_kwh": DEFAULT_CO2, "source": "default"}
        return price, co2

    def _local(self, ts: float) -> datetime:
        try:
            tz = self.core.reports.tz()
        except Exception:
            tz = None
        return datetime.fromtimestamp(ts, tz)

    def day_key(self, ts: float) -> str:
        return self._local(ts).strftime("%Y-%m-%d")

    def _midnight(self, ts: float) -> float:
        d = self._local(ts)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    @staticmethod
    def flows(home: float, solar: float, battery: float, grid: float) -> list:
        """Flows that add up. ``battery`` > 0 discharges into the home, < 0 charges; ``grid`` > 0 imports, < 0 exports."""
        home, solar = max(0.0, home), max(0.0, solar)
        dis, chg = max(0.0, battery), max(0.0, -battery)
        solar_home = min(solar, home)
        surplus = solar - solar_home
        solar_batt = min(surplus, chg)
        solar_grid = surplus - solar_batt
        batt_home = min(dis, home - solar_home)
        need = home - solar_home - batt_home
        imp = max(0.0, grid)
        grid_home = min(imp, need)
        grid_batt = min(max(0.0, imp - grid_home), chg - solar_batt)
        out = []
        for fid, a, b, w in (("grid-home", "grid", "home", grid_home), ("solar-home", "solar", "home", solar_home),
                             ("solar-grid", "solar", "grid", solar_grid), ("solar-battery", "solar", "battery", solar_batt),
                             ("battery-home", "battery", "home", batt_home), ("grid-battery", "grid", "battery", grid_batt)):
            if w > MIN_FLOW_W:
                out.append({"id": fid, "from": a, "to": b, "w": round(w, 1)})
        return out

    def _reading(self, cl: dict) -> dict:
        """The instantaneous figures in watts (None when a part has no reading)."""
        pw = cl["power"]
        grid, _ = self._sum(pw["grid"])
        exp, _ = self._sum(pw["export"])
        if grid is not None and exp is not None:
            grid -= exp
        elif grid is None and exp is not None:
            grid = -exp
        solar, _ = self._sum(pw["solar"])
        if solar is not None:
            solar = max(0.0, solar)             # an inverter on standby reports a few negative watts at night: no solar is 0, never a load
        batt, _ = self._sum(pw["battery"])
        hrow, _ = self._sum(pw["home"])
        lanes = [r for r in pw[None] if r["value"] is not None and r["value"] >= MIN_FLOW_W]
        lanes.sort(key=lambda r: (-r["value"], r["entity_id"]))
        lane_sum = sum(r["value"] for r in lanes)
        if grid is not None:
            home = grid + (solar or 0.0) + (batt or 0.0)
        elif hrow is not None:
            home = hrow
        else:
            home = lane_sum if lanes else None
        if home is not None:
            home = max(0.0, home)
        return {"grid": grid, "solar": solar, "battery": batt, "home": home, "lanes": lanes, "lane_sum": lane_sum}

    def _net_id(self, merge: dict, row: dict) -> Optional[str]:
        if not row["device"]:
            return None
        return (merge.get("by_hub") or {}).get(hubmerge.hub_key(row["integ"], row["device"]))

    def _fresh(self, cl: dict, now: float) -> dict:
        ts = [r["t"] for rows in cl["power"].values() for r in rows if r["value"] is not None and r["t"]]
        if not ts:
            return {"age_s": None, "level": "none"}
        age = max(0.0, now - max(ts))
        return {"age_s": round(age, 1), "level": levels.freshness(age)}

    def view(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        st = self.settings()
        cl = self.classify()
        pw = cl["power"]
        available = any(pw[r] for r in pw)
        price, co2 = self._rates(cl, st)
        out = {"t": now, "available": available,
               "settings": dict(st),
               "price": price, "co2": co2,
               "now": {"grid_w": None, "solar_w": None, "battery_w": None, "home_w": None},
               "flows": [], "lanes": [], "rest_w": None,
               "today": {"kwh": None, "source": None, "cost": None, "co2_kg": None, "solar_kwh": None, "export_kwh": None, "since": None},
               "fresh": {"age_s": None, "level": "none"}}
        if not available:
            return out
        rd = self._reading(cl)
        r1 = lambda v: None if v is None else round(v, 1)  # noqa: E731
        out["now"] = {"grid_w": r1(rd["grid"]), "solar_w": r1(rd["solar"]), "battery_w": r1(rd["battery"]), "home_w": r1(rd["home"])}
        home = rd["home"]
        if home is not None:
            grid = rd["grid"] if rd["grid"] is not None else home - (rd["solar"] or 0.0) - (rd["battery"] or 0.0)
            out["flows"] = self.flows(home, rd["solar"] or 0.0, rd["battery"] or 0.0, grid)
        try:
            merge = self.core.hub_merge() if getattr(self.core, "hub", None) else {}
        except Exception:
            merge = {}
        pv = price["value"] if price else None
        # what a lane costs and emits is its share of what the grid supplies right now: power from the own solar roof or the
        # battery costs nothing and emits nothing at the moment it is used (without a grid reading everything counts)
        share = 1.0
        if home and rd["grid"] is not None:
            share = max(0.0, min(1.0, max(0.0, rd["grid"]) / home))
        out["grid_share"] = round(share, 4)
        for r in rd["lanes"][:MAX_LANES]:
            out["lanes"].append({"id": r["entity_id"], "name": r["name"] or r["entity_id"], "area": r["area_name"] or None,
                                 "device": r["device"], "net_id": self._net_id(merge, r), "w": round(r["value"], 1),
                                 "cost_h": None if pv is None else round(r["value"] / 1000.0 * pv * share, 4),
                                 "co2_h_g": round(r["value"] / 1000.0 * co2["g_kwh"] * share, 2),
                                 "share": None if not home else round(min(1.0, r["value"] / home), 4)})
        if home is not None:
            out["rest_w"] = round(max(0.0, home - rd["lane_sum"]), 1)
        out["today"] = self._today(cl, st, price, co2, now)
        out["fresh"] = self._fresh(cl, now)
        return out

    # ------------------------------------------------------------------ today
    def _counter(self, cl: dict):
        lane_devices = {r["device"] for r in cl["power"][None] if r["device"]}
        mine = [c for c in cl["counters"] if c["role"] in (None, "home") and c["device"] not in lane_devices]
        mine.sort(key=lambda c: (len(c["text"].split()), c["entity_id"]))
        return mine[0] if mine else None

    @staticmethod
    def _money(imported, export, price, feed_in, g):
        cost = None
        if price is not None:
            cost = imported * price - (export * feed_in if feed_in else 0.0)
        return cost, imported * g / 1000.0

    def _today(self, cl: dict, st: dict, price, co2, now: float) -> dict:
        t = {"kwh": None, "source": None, "cost": None, "co2_kg": None, "solar_kwh": None, "export_kwh": None, "since": None}
        counter = self._counter(cl)
        if counter is not None:
            solar = next((c["value"] for c in cl["counters"] if c["role"] == "solar"), None)
            export = next((c["value"] for c in cl["counters"] if c["role"] == "export"), None)
            kwh = counter["value"]
            self_use = max(0.0, (solar or 0.0) - (export or 0.0))
            imported = max(0.0, kwh - self_use)
            cost, co2_kg = self._money(imported, export or 0.0, price["value"] if price else None, st["feed_in"], co2["g_kwh"])
            t.update(kwh=round(kwh, 2), source="entity", cost=None if cost is None else round(cost, 2), co2_kg=round(co2_kg, 2),
                     solar_kwh=None if solar is None else round(solar, 2), export_kwh=None if export is None else round(export, 2),
                     since=self._midnight(now))
            return t
        with self._lock:
            rec = self._load().get(self.day_key(now))
            if rec and rec.get("kwh") is not None:
                t.update(kwh=round(rec["kwh"], 2), source="counted", cost=None if rec.get("cost") is None else round(rec["cost"], 2),
                         co2_kg=round(rec.get("co2_kg") or 0.0, 2),
                         solar_kwh=round(rec["solar_kwh"], 2) if rec.get("solar_kwh") is not None else None,
                         export_kwh=round(rec["export_kwh"], 2) if rec.get("export_kwh") is not None else None, since=rec.get("since"))
        return t

    # ------------------------------------------------------------------ counting
    def _load(self) -> dict:
        if self._days is None:
            raw = self.core.store.get_setting(DAYS_SETTING, {})
            self._days = raw if isinstance(raw, dict) else {}
        return self._days

    def _save(self, now: float, force: bool = False) -> None:
        days = self._load()
        for k in sorted(days)[:-KEEP_DAYS]:
            days.pop(k, None)
        if force or now - self._saved_t >= SAVE_EVERY_S:
            self.core.store.set_setting(DAYS_SETTING, days)
            self._saved_t = now

    def step(self, now: Optional[float] = None) -> None:
        """Count one sample. With a "today" counter in the hub that value is the day's total (the last one before midnight stays)."""
        now = self.clock() if now is None else now
        try:
            cl = self.classify()
            if not any(cl["power"][r] for r in cl["power"]):
                self._last = None
                return
            st = self.settings()
            price, co2 = self._rates(cl, st)
            day = self.day_key(now)
            with self._lock:
                days = self._load()
                changed_day = self._saved_day is not None and self._saved_day != day
                self._saved_day = day
                counter = self._counter(cl)
                if counter is not None:
                    tdy = self._today(cl, st, price, co2, now)
                    days[day] = {"kwh": tdy["kwh"], "cost": tdy["cost"], "co2_kg": tdy["co2_kg"], "solar_kwh": tdy["solar_kwh"],
                                 "export_kwh": tdy["export_kwh"], "since": tdy["since"]}
                    self._last = None
                    self._save(now, changed_day)
                    return
                rd = self._reading(cl)
                if rd["home"] is None:
                    self._last = None
                    return
                solar = max(0.0, rd["solar"] or 0.0)
                grid = rd["grid"] if rd["grid"] is not None else rd["home"] - solar - (rd["battery"] or 0.0)
                cur = (now, rd["home"], solar, max(0.0, -grid), max(0.0, grid), price["value"] if price else None, st["feed_in"], co2["g_kwh"])
                last = self._last
                self._last = cur
                if last is None or not 0 < now - last[0] <= GAP_S:
                    self._save(now, changed_day)
                    return
                dt_h = (now - last[0]) / 3600.0
                avg = [(a + b) / 2.0 for a, b in zip(last[1:5], cur[1:5])]
                rec = days.setdefault(day, {"kwh": 0.0, "cost": None, "co2_kg": 0.0, "solar_kwh": 0.0, "export_kwh": 0.0, "since": last[0]})
                rec["kwh"] = (rec.get("kwh") or 0.0) + avg[0] / 1000.0 * dt_h
                rec["solar_kwh"] = (rec.get("solar_kwh") or 0.0) + avg[1] / 1000.0 * dt_h
                rec["export_kwh"] = (rec.get("export_kwh") or 0.0) + avg[2] / 1000.0 * dt_h
                imported = avg[3] / 1000.0 * dt_h
                rec["co2_kg"] = (rec.get("co2_kg") or 0.0) + imported * cur[7] / 1000.0
                if cur[5] is not None:
                    rec["cost"] = (rec.get("cost") or 0.0) + imported * cur[5] - (avg[2] / 1000.0 * dt_h * cur[6] if cur[6] else 0.0)
                self._save(now, changed_day)
        except Exception:
            log.exception("energy step failed")

    def day_totals(self, day: str) -> Optional[dict]:
        """{kwh, cost, co2_kg, solar_kwh, export_kwh} of a local day ("YYYY-MM-DD") or None."""
        with self._lock:
            rec = self._load().get(day)
            if not rec or rec.get("kwh") is None:
                return None
            return {k: rec.get(k) for k in ("kwh", "cost", "co2_kg", "solar_kwh", "export_kwh")}

    def days(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._load().items()}

    def yesterday_key(self, until: float) -> str:
        return (self._local(until) - timedelta(days=1)).strftime("%Y-%m-%d")
