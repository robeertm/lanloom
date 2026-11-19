"""Network routes (DW2): which networks and places the data passes towards the most important destinations. An option,
switched off by default (*Settings > General > Network routes*).

With the option off nothing happens at all: no destination is remembered, no probe runs, not one packet is sent.

With it on:
  * Destinations: while the data paths book a flow, the destination is remembered in memory only, by country, operator
    and city (the level of the data paths) with its bytes (decaying over a day) and one current address of that group.
    The address is used only for the next measurement and is never written anywhere; a restart forgets it.
  * Schedule: the ``targets`` groups with the most bytes (5 by default, at most 20), each at most every ``interval_h``
    hours (6 by default, at least 1). A run never starts while the machine is busy (load above 85 % of the processor
    cores, or a scan running) and stops at a hard time budget; the probes run in a worker thread of their own with the
    lowest CPU priority (``routeprobe.py``; the kernel keeps a nice value per thread, no program is started), one probe
    per hop, one destination after the other.
  * Methods, best first: ``udp`` (Paris traceroute over an ordinary UDP socket, no privileges), ``tcp`` (connection
    attempts to port 443, no privileges). Without either the option says "not possible here" with the reason and the rest
    of the app is untouched.
  * Reading: ``routes.build_chain`` - per hop network and operator (from the data paths' city and operator data, if that
    option is on), places from router names (airport and city codes, ``lanloom/data/route-places.json``), exchange points
    (router names, optionally PeeringDB, ``ixp.py``), every place checked against the round-trip time.
  * Stored (table ``paths_route``, 30 days): per destination group only the condensed chain - kind, AS number, operator,
    country, place (city), round-trip band per section - and a signature per day for "route changed" (compared with the
    day before; a calm note, never an alarm). No router address, no host name, no destination address, not in the database,
    not in an export, not in a log.
"""
import hashlib
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import routeprobe, routes
from .ixp import Exchanges

log = logging.getLogger("lanloom")

TARGETS_DEFAULT, TARGETS_MAX = 5, 20
INTERVAL_DEFAULT_H, INTERVAL_MIN_H, INTERVAL_MAX_H = 6, 1, 168
KEEP_DAYS = 30
TICK_S = 60.0
RUN_BUDGET_S = 150.0             # one run (all due destinations) never takes longer
TARGET_BUDGET_S = 30.0           # one destination
HELPER_GRACE_S = 5.0             # the probe thread is given up this long after its own budget (it ends by its budget)
PAUSE_S = 2.0                    # between two destinations of a run
LOAD_LIMIT = 0.85                # share of the processor cores; above it no run starts
CHECK_EVERY_S = 6 * 3600         # how often the possible methods are checked again
MAX_CANDIDATES = 3000
TAU_S = 86400.0                  # bytes of a destination group decay with this time constant
MAX_HOPS, WAIT_S, GAP = 30, 1.0, 4
METHOD_ORDER = ("udp", "tcp")
PLACES_FILE = Path(__file__).parent / "data" / "route-places.json"
ANYCAST = "~anycast"

SCHEMA = """
CREATE TABLE IF NOT EXISTS paths_route (key TEXT PRIMARY KEY, cc TEXT NOT NULL, asn INTEGER NOT NULL DEFAULT 0,
    city TEXT NOT NULL DEFAULT '', t REAL NOT NULL, method TEXT NOT NULL, sections TEXT NOT NULL, sig TEXT NOT NULL,
    changed REAL, prev TEXT);
CREATE TABLE IF NOT EXISTS paths_route_day (day INTEGER NOT NULL, key TEXT NOT NULL, sig TEXT NOT NULL,
    PRIMARY KEY (day, key));
"""


def route_key(cc: str, asn: int, city: str) -> str:
    """Stable key of a destination group (country, operator, city) - without any address."""
    h = hashlib.sha256(("%s|%d|%s" % (cc, int(asn or 0), city or "")).encode()).hexdigest()[:10]
    return "%s:%d:%s" % (cc if cc != ANYCAST else "AC", int(asn or 0), h)


def clamp_int(value, lo, hi, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def load_places(path: Path = PLACES_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        p = d.get("places") if isinstance(d, dict) else None
        return p if isinstance(p, dict) else {}
    except (OSError, ValueError):
        return {}


def run_helper(request: dict, timeout: float) -> dict:
    """Run the probe in a worker thread of its own with the lowest CPU priority (nice 19 for this thread only) and wait at
    most ``timeout`` seconds; the probe keeps its own, shorter budget, so a thread that is given up ends by itself."""
    out: dict = {}

    def work():
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except (OSError, AttributeError):
            pass
        try:
            out.update(routeprobe.check() if request.get("mode") == "check" else routeprobe.measure(request))
        except Exception:                  # the probe must never take the app down
            out["error"] = "probe"
    t = threading.Thread(target=work, name="lanloom-route-probe", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return {"error": "timeout"}
    return dict(out)


class RouteWatch:
    def __init__(self, core, folder: Optional[Path] = None, helper: Callable[[dict, float], dict] = run_helper,
                 clock: Callable[[], float] = time.time, loadavg: Optional[Callable[[], tuple]] = None, cpus: Optional[int] = None):
        self.core = core
        self.store = core.store
        self.helper = helper
        self.clock = clock
        self.loadavg = loadavg or (os.getloadavg if hasattr(os, "getloadavg") else (lambda: (0.0, 0.0, 0.0)))
        self.cpus = cpus or os.cpu_count() or 1
        self._places = None
        self.exchanges = Exchanges((folder / "routes") if folder else None, None, clock=clock)
        self.candidates: dict = {}         # key -> {"cc", "asn", "city", "anycast", "bytes", "t", "ips": {ip: bytes}}  (memory only)
        self.measured: dict = {}           # key -> time of the last attempt (memory; the table has the last success)
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.caps: Optional[dict] = None
        self.caps_t = 0.0
        self.force = False
        self.thread: Optional[threading.Thread] = None
        self.stopping = threading.Event()
        self.state = {"running": False, "last_run": None, "last_error": None, "probes": 0, "measured": 0, "skipped": None,
                      "method": None}
        self.demo = False
        self.remote = bool(getattr(core, "remote", False))
        self._wants, self._wants_t = False, -1e9
        with self.store.transaction() as tx:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    tx._db.execute(stmt)

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        s = self.core.settings()
        return {"enabled": bool(s.get("paths_routes")) and not self.remote,
                "targets": clamp_int(s.get("paths_routes_targets"), 1, TARGETS_MAX, TARGETS_DEFAULT),
                "interval_h": clamp_int(s.get("paths_routes_interval_h"), INTERVAL_MIN_H, INTERVAL_MAX_H, INTERVAL_DEFAULT_H),
                "peeringdb": bool(s.get("paths_routes_peeringdb"))}

    @property
    def places(self) -> dict:
        if self._places is None:
            self._places = load_places()
            self.exchanges.places = self._places
            if self.exchanges.table is not None:
                self.exchanges.load()
        return self._places

    def apply_settings(self) -> None:
        """After a settings change: the exchange copy follows its switch; switched off, the memory is emptied."""
        st = self.settings()
        self.exchanges.configure(st["enabled"] and st["peeringdb"] and not self.demo)
        if not st["enabled"]:
            with self.lock:
                self.candidates.clear()
                self.measured.clear()
        self.caps_t = 0.0
        self._wants_t = -1e9

    # ------------------------------------------------------------------ destinations (fed by the data paths)
    def wants(self) -> bool:
        """Should the data paths hand over destinations? Only with the option on (otherwise nothing is remembered).
        Asked for every booked flow, so the answer is kept for a few seconds (a settings change resets it)."""
        t = time.monotonic()
        if t - self._wants_t > 5.0:
            self._wants = self.settings()["enabled"] and not self.demo
            self._wants_t = t
        return self._wants

    def note(self, peer: str, cat: str, asn: int, city: str, nbytes: int, now: Optional[float] = None) -> None:
        """One booked flow: country (or anycast), operator and city of the destination, its bytes and its address."""
        if not peer or not nbytes or cat.startswith("~") and cat != ANYCAST:
            return
        now = self.clock() if now is None else now
        anycast = cat == ANYCAST
        key = route_key(cat, 0 if anycast else asn, "" if anycast else city)
        with self.lock:
            c = self.candidates.get(key)
            if c is None:
                if len(self.candidates) >= MAX_CANDIDATES:
                    self._shrink(now)
                c = self.candidates[key] = {"cc": None if anycast else cat, "asn": 0 if anycast else int(asn or 0),
                                            "city": "" if anycast else (city or ""), "anycast": anycast, "bytes": 0.0, "t": now, "ips": {}}
            c["bytes"] = c["bytes"] * math.exp(-max(0.0, now - c["t"]) / TAU_S) + nbytes
            c["t"] = now
            ips = c["ips"]
            ips[peer] = ips.get(peer, 0) + nbytes
            if len(ips) > 4:                          # keep the four biggest addresses of the group, nothing more
                for ip in sorted(ips, key=ips.get)[:len(ips) - 4]:
                    del ips[ip]

    def _shrink(self, now: float) -> None:
        def val(item):
            return item[1]["bytes"] * math.exp(-max(0.0, now - item[1]["t"]) / TAU_S)
        keep = sorted(self.candidates.items(), key=val, reverse=True)[:MAX_CANDIDATES // 2]
        self.candidates = dict(keep)

    def top(self, n: int, now: Optional[float] = None) -> list:
        now = self.clock() if now is None else now
        with self.lock:
            items = [(k, dict(c, value=c["bytes"] * math.exp(-max(0.0, now - c["t"]) / TAU_S), ip=max(c["ips"], key=c["ips"].get)))
                     for k, c in self.candidates.items() if c["ips"]]
        items.sort(key=lambda x: (-x[1]["value"], x[0]))
        return items[:n]

    def due(self, now: Optional[float] = None) -> list:
        """The destination groups to measure now (most bytes first)."""
        now = self.clock() if now is None else now
        st = self.settings()
        out = []
        for key, c in self.top(st["targets"], now):
            last = self.measured.get(key, 0.0)
            if self.force or now - last >= st["interval_h"] * 3600:
                out.append((key, c))
        return out

    # ------------------------------------------------------------------ what is possible here
    def capabilities(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        if self.caps is not None and now - self.caps_t < CHECK_EVERY_S:
            return self.caps
        if os.name != "posix":
            self.caps = {"methods": {"udp": False, "tcp": False}, "reasons": {"all": "platform"}}
        else:
            res = self.helper({"mode": "check"}, 20.0)
            methods = res.get("methods") if isinstance(res.get("methods"), dict) else {}
            self.caps = {"methods": {m: methods.get(m) is True for m in METHOD_ORDER},
                         "reasons": {k: str(v)[:40] for k, v in (res.get("reasons") or {}).items() if isinstance(k, str)} if isinstance(res.get("reasons"), dict) else {}}
            if res.get("error"):
                self.caps["reasons"]["all"] = str(res["error"])[:40]
        self.caps_t = now
        return self.caps

    def methods(self) -> list:
        caps = self.capabilities()
        return [m for m in METHOD_ORDER if caps["methods"].get(m)]

    def busy(self) -> Optional[str]:
        """Reason not to start a run now, or None."""
        try:
            load = float(self.loadavg()[0])
        except (OSError, IndexError, TypeError, ValueError):
            load = 0.0
        if load > LOAD_LIMIT * self.cpus:
            return "load"
        scan = getattr(self.core, "scan_state", None) or {}
        if scan.get("state") == "running":
            return "scan"
        return None

    # ------------------------------------------------------------------ one measurement
    def _geo(self, ip: str) -> dict:
        geo = self.core.geo
        cc = geo.country(ip)
        out = {"cc": cc if cc and cc != "ZZ" else None}
        if out["cc"]:
            hit = geo.detail(ip, cc) if hasattr(geo, "detail") else None
            if hit:
                _key, asn, place, operator = hit
                out["asn"] = asn or None
                out["operator"] = operator or None
                if place:
                    out["place"] = {"name": place.get("name"), "lat": place.get("lat"), "lon": place.get("lon"), "cc": cc}
        return out

    def _target(self, c: dict) -> dict:
        """What the chain may say about the destination: country, operator, city - never its address."""
        if c.get("anycast"):
            return {"anycast": True}
        name = place = None
        if c.get("city"):
            row = self.store.one("SELECT name, lat, lon FROM paths_place WHERE cc=? AND city=?", (c["cc"], c["city"]))
            if row:
                name = row["name"]
                place = {"name": row["name"], "lat": row["lat"], "lon": row["lon"]}
        op = None
        if c.get("asn"):
            row = self.store.one("SELECT name FROM paths_operator WHERE asn=?", (c["asn"],))
            op = row["name"] if row else None
        return {"cc": c["cc"], "asn": c.get("asn") or None, "operator": op, "city": name, "place": place}

    def measure_one(self, key: str, c: dict, methods: list, deadline: float) -> Optional[dict]:
        """Measure one destination group with the best method that answers; store the condensed chain. Returns the row."""
        ip = c["ip"]
        hops, used, probes = [], None, 0
        for m in methods:
            left = deadline - self.clock()
            if left < 3:
                break
            budget = min(TARGET_BUDGET_S, left - 1)
            req = {"mode": "measure", "method": m, "target": ip, "max_hops": MAX_HOPS, "wait": WAIT_S, "gap": GAP,
                   "budget": budget, "names": True, "port": 443}
            res = self.helper(req, budget + HELPER_GRACE_S)
            probes += int(res.get("probes") or 0) if isinstance(res.get("probes"), (int, float)) else 0
            if res.get("error") and not res.get("hops"):
                self.state["last_error"] = str(res["error"])[:40]
                continue
            hops = routes.read_probe(json.dumps({"hops": res.get("hops") or []}))
            if any(h.get("ip") and not routes.is_home_ip(h["ip"]) for h in hops):
                used = m
                break
        ip = None                                       # the address is not needed any more
        self.state["probes"] += probes
        if not used:
            return None
        target = self._target(c)
        start = {"cc": (self.core.settings().get("home_country") or "").upper() or None}
        chain = routes.build_chain(hops, self._geo, self.exchanges if self.exchanges.enabled else None, self.places, start, target)
        hops = None
        sections = routes.condense(chain)
        return self.save(key, c, used, sections)

    def save(self, key: str, c: dict, method: str, sections: list, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        sig = routes.signature(sections)
        day = int(now // 86400)
        old = self.store.one("SELECT sig, changed, prev FROM paths_route WHERE key=?", (key,))
        before = self.store.one("SELECT sig FROM paths_route_day WHERE key=? AND day<? ORDER BY day DESC LIMIT 1", (key, day))
        changed, prev = (old["changed"], old["prev"]) if old else (None, None)
        if before and before["sig"] != sig and (not old or old["sig"] != sig or changed is None):
            changed, prev = now, before["sig"]
        elif before and before["sig"] == sig:
            changed, prev = None, None
        with self.store.transaction() as tx:
            tx._db.execute("INSERT OR REPLACE INTO paths_route(key, cc, asn, city, t, method, sections, sig, changed, prev) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (key, c["cc"] or ANYCAST, int(c.get("asn") or 0), c.get("city") or "", now, method,
                            json.dumps(sections, separators=(",", ":")), sig, changed, prev))
            tx._db.execute("INSERT OR REPLACE INTO paths_route_day(day, key, sig) VALUES(?,?,?)", (day, key, sig))
        return {"key": key, "sig": sig, "changed": changed}

    def prune(self, now: Optional[float] = None) -> int:
        now = self.clock() if now is None else now
        with self.store.transaction() as tx:
            a = tx._db.execute("DELETE FROM paths_route WHERE t < ?", (now - KEEP_DAYS * 86400,)).rowcount
            b = tx._db.execute("DELETE FROM paths_route_day WHERE day < ?", (int(now // 86400) - KEEP_DAYS,)).rowcount
        return (a or 0) + (b or 0)

    def clear(self) -> None:
        with self.lock:
            self.candidates.clear()
            self.measured.clear()
        with self.store.transaction() as tx:
            tx._db.execute("DELETE FROM paths_route")
            tx._db.execute("DELETE FROM paths_route_day")

    # ------------------------------------------------------------------ runs
    def run_once(self, now: Optional[float] = None) -> dict:
        """One run: due destinations one after the other until the budget is used up. Nothing at all with the option off."""
        st = self.settings()
        if not st["enabled"] or self.demo:
            return {"ran": False, "reason": "off"}
        if not self.run_lock.acquire(blocking=False):
            return {"ran": False, "reason": "running"}
        try:
            due = self.due(now)
            if not due:
                self.force = False
                return {"ran": False, "reason": "nothing_due"}
            reason = self.busy()
            if reason:
                self.state["skipped"] = reason
                return {"ran": False, "reason": reason}
            methods = self.methods()
            if not methods:
                return {"ran": False, "reason": "impossible"}
            self.state.update(running=True, skipped=None, probes=0, measured=0, last_error=None)
            start = self.clock()
            deadline = start + RUN_BUDGET_S
            self.exchanges.step()
            for i, (key, c) in enumerate(due):
                if self.stopping.is_set() or deadline - self.clock() < 5:
                    break
                if i and self.busy():
                    self.state["skipped"] = "load"
                    break
                self.measured[key] = self.clock()
                row = self.measure_one(key, c, methods, deadline)
                if row:
                    self.state["measured"] += 1
                    self.state["method"] = self.store.one("SELECT method FROM paths_route WHERE key=?", (key,))["method"]
                if i < len(due) - 1:
                    self.stopping.wait(PAUSE_S)
            self.force = False
            self.prune()
            self.state["last_run"] = self.clock()
            return {"ran": True, "measured": self.state["measured"], "probes": self.state["probes"]}
        finally:
            self.state["running"] = False
            self.run_lock.release()

    def measure_now(self) -> dict:
        """Operator action: measure the most important destinations now (still never while the machine is busy)."""
        st = self.settings()
        if not st["enabled"]:
            raise ValueError("paths.routes.off")
        if self.demo:
            return self.status()
        self.force = True
        threading.Thread(target=self.run_once, name="lanloom-routes-now", daemon=True).start()
        return self.status()

    def start(self) -> None:
        if self.thread or self.demo or self.remote:
            return
        self.apply_settings()
        self.thread = threading.Thread(target=self._loop, name="lanloom-routes", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()

    def _loop(self) -> None:
        while not self.stopping.wait(TICK_S):
            try:
                if self.settings()["enabled"]:
                    self.run_once()
            except Exception:              # the measurement must never take the app down
                log.exception("network routes run failed")

    # ------------------------------------------------------------------ reading
    def rows(self, cc: Optional[str] = None, limit: int = 40) -> list:
        """Stored routes (condensed), newest first; optionally only those towards one country."""
        if cc:
            rs = self.store.query("SELECT * FROM paths_route WHERE cc=? ORDER BY t DESC LIMIT ?", (cc, limit))
        else:
            rs = self.store.query("SELECT * FROM paths_route ORDER BY t DESC LIMIT ?", (limit,))
        now = self.clock()
        out = []
        for r in rs:
            try:
                sections = json.loads(r["sections"])
            except ValueError:
                continue
            tgt = next((s for s in sections if s.get("kind") == "target"), {})
            rtt = tgt.get("rtt") if tgt.get("rtt") is not None else next((s["rtt"] for s in reversed(sections) if s.get("rtt") is not None), None)
            out.append({"key": r["key"], "cc": None if r["cc"] == ANYCAST else r["cc"], "anycast": r["cc"] == ANYCAST,
                        "asn": r["asn"] or None, "operator": tgt.get("operator"), "city": tgt.get("city"), "t": r["t"],
                        "age_s": max(0, int(now - r["t"])), "method": r["method"], "sections": sections, "rtt": rtt,
                        "reached": bool(tgt.get("reached")), "uncertain": routes.uncertain(sections),
                        "changed": r["changed"], "changed_recent": bool(r["changed"] and now - r["changed"] < 36 * 3600)})
        return out

    def changes_between(self, since: float, until: float) -> list:
        return [r for r in self.rows(limit=200) if r["changed"] and since <= r["changed"] < until]

    def places_for(self, cc: str) -> dict:
        """AS number -> place of the destination network for the groups of one country without a city (DW1 draws that
        traffic as an approximate bubble; a measured route knows a better place). Only sure places inside the country."""
        out = {}
        for r in self.store.query("SELECT asn, sections FROM paths_route WHERE cc=? AND city=''", (cc,)):
            try:
                sections = json.loads(r["sections"])
            except ValueError:
                continue
            for s in reversed(sections):
                p = s.get("place")
                if s.get("kind") in ("network", "target", "transit", "exchange", "provider") and p and p.get("sure") and p.get("cc") == cc:
                    out[int(r["asn"] or 0)] = {"name": p.get("name") or "", "lat": p["lat"], "lon": p["lon"]}
                    break
        return out

    def status(self) -> dict:
        st = self.settings()
        out = {"enabled": st["enabled"], "targets": st["targets"], "interval_h": st["interval_h"], "peeringdb": st["peeringdb"],
               "limits": {"targets_max": TARGETS_MAX, "interval_min_h": INTERVAL_MIN_H, "interval_max_h": INTERVAL_MAX_H,
                          "run_budget_s": int(RUN_BUDGET_S), "max_hops": MAX_HOPS},
               "running": self.state["running"], "last_run": self.state["last_run"], "last_error": self.state["last_error"],
               "skipped": self.state["skipped"], "probes_last_run": self.state["probes"], "method": self.state["method"],
               "exchanges": self.exchanges.info(), "stored": (self.store.one("SELECT COUNT(*) AS n FROM paths_route") or {"n": 0})["n"]}
        if self.demo:
            out.update(possible=True, methods={"udp": True, "tcp": True}, reason=None, demo=True, method="udp")
            return out
        if self.remote:
            out.update(possible=False, methods={}, reason="remote")
            return out
        if not st["enabled"]:
            out.update(possible=None, methods={}, reason=None)       # off: nothing is checked (no probe runs)
            return out
        caps = self.capabilities()
        ok = [m for m in METHOD_ORDER if caps["methods"].get(m)]
        out.update(possible=bool(ok), methods=caps["methods"], reason=None if ok else (caps["reasons"].get("all") or caps["reasons"].get("udp") or "sockets"))
        if out["method"] is None and ok:
            out["method"] = ok[0]
        return out
