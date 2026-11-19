"""Battery backup (UPS): what the Network UPS Tools server or the UPS-MIB of an SNMP agent reports, read only.

Neutral reading of one UPS (``lanloom/sources/nut.py`` and ``snmp.parse_ups_mib`` both produce it)::

    {name, description, status, charge, charge_low, runtime_s, runtime_low_s, load, input_v, output_v, battery_v, temp,
     power_w, test, model}

``status`` is the NUT word list (``OL`` online, ``OB`` on battery, ``LB`` low battery, ``CHRG``, ``DISCHRG``, ``RB`` replace
battery, ``OVER`` overload, ``BYPASS`` ...). The state Lanloom shows:

    online | charging | battery | low_battery | replace | overload | unknown

A UPS that has not been heard from for 15 minutes is ``unknown`` (never ``ok``). Remaining runtime is a figure the UPS
estimates; Lanloom shows it as reported ("about 38 min") and says so when the mains are fine ("if the power fails now").
Every change of state is written to ``ups_log`` (35 days) so the nightly report can say "on battery from 03:12 to 03:19".
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from .levels import STALE_S, freshness, num

KEEP_DAYS = 35
SCHEMA = """
CREATE TABLE IF NOT EXISTS ups_log (t REAL NOT NULL, unit TEXT NOT NULL, state TEXT NOT NULL, charge REAL, runtime_s REAL);
CREATE INDEX IF NOT EXISTS ups_log_t ON ups_log(t);
"""
LOW_RUNTIME_S = 600.0              # estimated runtime below this on battery counts as "about to run out"
LOW_CHARGE = 30.0


def state_of(status: str, charge=None, runtime_s=None, charge_low=None) -> str:
    words = set(str(status or "").upper().replace(",", " ").split())
    if not words:
        return "unknown"
    if "LB" in words:
        return "low_battery"
    if "OB" in words or "DISCHRG" in words:
        low = charge_low if charge_low is not None else LOW_CHARGE
        if (charge is not None and charge <= low) or (runtime_s is not None and runtime_s <= LOW_RUNTIME_S / 5):
            return "low_battery"
        return "battery"
    if "OVER" in words:
        return "overload"
    if "RB" in words:
        return "replace"
    if "CHRG" in words:
        return "charging"
    if "OL" in words or "BYPASS" in words or "TRIM" in words or "BOOST" in words:
        return "online"
    return "unknown"


LEVEL = {"online": "ok", "charging": "ok", "battery": "warn", "low_battery": "danger", "replace": "warn", "overload": "warn", "unknown": "unknown"}


class UpsBook:
    def __init__(self, core, clock=time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self._lock = threading.RLock()
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.store.execute(stmt)
        self.units: dict = {}          # key -> {"t", "data", "source", "integ", "host", "state", "since"}

    # ------------------------------------------------------------------ feeding
    def feed(self, source: str, integ_id, host: str, readings: list, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            for r in readings:
                if not isinstance(r, dict):
                    continue
                key = "%s:%s:%s" % (source, integ_id, r.get("name") or "ups")
                st = state_of(r.get("status"), num(r.get("charge")), num(r.get("runtime_s")), num(r.get("charge_low")))
                cur = self.units.get(key)
                since = cur["since"] if cur and cur["state"] == st else now
                if cur is None or cur["state"] != st:
                    self._log(key, st, r, now)
                self.units[key] = {"t": now, "data": r, "source": source, "integ": integ_id, "host": host, "state": st, "since": since,
                                   "key": key}

    def _log(self, key: str, state: str, r: dict, now: float) -> None:
        try:
            self.store.execute("INSERT INTO ups_log(t, unit, state, charge, runtime_s) VALUES (?,?,?,?,?)",
                               (now, key, state, num(r.get("charge")), num(r.get("runtime_s"))))
            self.store.execute("DELETE FROM ups_log WHERE t < ?", (now - KEEP_DAYS * 86400,))
        except Exception:
            pass

    def forget(self, integ_id) -> None:
        with self._lock:
            for k in [k for k, u in self.units.items() if u["integ"] == integ_id]:
                self.units.pop(k, None)

    # ------------------------------------------------------------------ views
    def view(self, now: Optional[float] = None) -> list:
        """The units as the page shows them, worst first."""
        now = self.clock() if now is None else now
        out = []
        with self._lock:
            units = list(self.units.values())
        for u in units:
            d = u["data"]
            age = max(0.0, now - u["t"])
            fresh = freshness(age)
            state = u["state"] if fresh != "stale" else "unknown"
            charge, runtime = num(d.get("charge")), num(d.get("runtime_s"))
            dev = (self.core.ip_to_device.get(u["host"] or "") if getattr(self.core, "ip_to_device", None) else None)
            out.append({"id": u["key"], "name": d.get("description") or d.get("name") or "UPS", "ups": d.get("name"), "model": d.get("model") or "",
                        "device": dev, "state": state, "level": LEVEL.get(state, "unknown"), "fresh": fresh, "age_s": round(age, 1),
                        "status": d.get("status") or "", "charge": charge, "runtime_s": runtime,
                        "runtime_if_failed": state in ("online", "charging", "replace"), "load": num(d.get("load")),
                        "input_v": num(d.get("input_v")), "output_v": num(d.get("output_v")), "battery_v": num(d.get("battery_v")),
                        "temp": num(d.get("temp")), "power_w": num(d.get("power_w")), "test": d.get("test") or "", "source": u["source"],
                        "since_t": u["since"] if state != "unknown" else None,
                        "runtime_low": bool(runtime is not None and state in ("battery", "low_battery") and runtime <= LOW_RUNTIME_S)})
        out.sort(key=lambda r: ({"danger": 0, "warn": 1, "unknown": 2, "ok": 3}.get(r["level"], 4), r["name"].lower()))
        return out

    def runtime_for(self, device_id: Optional[str] = None, now: Optional[float] = None) -> Optional[dict]:
        """The best UPS to quote on a card: the one named for the device, else the worst fresh one."""
        rows = [r for r in self.view(now) if r["fresh"] != "stale"]
        mine = [r for r in rows if device_id and r["device"] == device_id]
        pool = mine or rows
        return pool[0] if pool else None

    def episodes(self, since: float, until: float) -> list:
        """[{unit, from_t, to_t, state, open}] where the UPS was not on mains, inside [since, until] (oldest first)."""
        rows = self.store.query("SELECT t, unit, state FROM ups_log WHERE t <= ? ORDER BY t", (until,))
        by: dict = {}
        for r in rows:
            by.setdefault(r["unit"], []).append((r["t"], r["state"]))
        out = []
        for unit, seq in by.items():
            start, kind = None, None
            for t, st in seq:
                bad = st in ("battery", "low_battery")
                if bad and start is None:
                    start, kind = t, st
                elif bad and st == "low_battery":
                    kind = st
                elif not bad and start is not None:
                    if t >= since:
                        out.append({"unit": unit, "from_t": max(start, since), "to_t": t, "state": kind, "open": False})
                    start = None
            if start is not None:
                out.append({"unit": unit, "from_t": max(start, since), "to_t": until, "state": kind, "open": True})
        out.sort(key=lambda e: e["from_t"])
        return out
