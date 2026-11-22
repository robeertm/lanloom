"""Overview (start page) and the "Right now" rail: how the network is doing, in three seconds.

Everything here is read from Lanloom's own state (inventory, live counters, source status, event journal, nightly
reports); nothing talks to the network and nothing is written except a small in-memory health history.

* ``summary(now)``  - key figures (devices, online, new since yesterday, sources fresh/stale, internet line, data paths in
  short, nightly report), the health with a short look back, the "noteworthy" list (one sub line per issue), new and
  unsure devices (first seen, origin class) and the freshness of every source with its last sign of life.
* ``right_now(now)`` - what is going on: active conversations (measured traces), recent changes, new devices.

Sources never show an error text when they simply have nothing yet. A source is in one of these calm states:
``fresh`` (data arrives in its usual rhythm), ``stale`` (data stopped coming), ``waiting`` (just started, nothing yet),
``nodata`` (connected, but it has not reported anything), ``failed`` (the connection fails; reason key), ``off``.

Later features add rows to the noteworthy list with ``register_note(fn)`` (``fn(core, now) -> [note, ...]``), e.g. open
incidents. A note is ``{"id", "level": "info"|"warn"|"danger", "key", "sub", "vars", "link", "human"}``; the client shows ``key``
as the line and ``sub`` (optional) as the sub line. ``human: True`` marks a row only a person can end: unchanged for 24 hours it
moves to the "Waiting on someone" line (``waiting``, lanloom/waiting.py) and no longer sets the overall level.
``forecasts`` are the early warnings of the host health (lanloom/hosthealth.py).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

from . import live
from .waiting import Waiting

HISTORY_STEP_S = 300            # one health sample every five minutes
HISTORY_LEN = 288               # 24 hours
NEW_WINDOW_S = 86400.0          # "new since yesterday"
FIRST_SCAN_GRACE_S = 900.0      # devices of the very first inventory are not "new"
CHANGES_WINDOW_S = 6 * 3600.0
MAX_CHANGES = 14
MAX_TALKS = 8
MAX_LIST = 8
TALK_FROM = 256.0               # bytes per second (both directions) before a trace counts as a conversation
CACHE_S = 2.0
PATHS_CACHE_S = 60.0
INFRA_KINDS = ("router", "switch", "ap", "nas", "server", "hub", "broker", "firewall")
LEVELS = ("ok", "info", "warn", "danger")
CHANGE_KINDS = ("dev_new", "dev_offline", "dev_back", "dev_gone", "src_fail", "src_ok", "wan_down", "wan_up", "wan_switch",
                "app_start", "fw_drift")

NOTES: list = []


def register_note(fn: Callable) -> Callable:
    """A later feature contributes rows to the noteworthy list (open incidents, warnings ...)."""
    if fn not in NOTES:
        NOTES.append(fn)
    return fn


def _rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def source_state(core, key: str, now: float, enabled: bool = True) -> dict:
    """Calm state of one source (see module doc) with grade, last sign of life and cadence."""
    if not enabled:
        return {"state": "off", "grade": "unknown", "age_s": None, "cadence_s": None}
    st = core.source_status.get(key)
    cad = core.cadence.get(key)
    last = cad.last if cad else None
    c = cad.cadence() if cad else None
    age = round(now - last, 1) if last else None
    grade = core._source_grade(key) if cad else "unknown"
    out = {"grade": grade, "age_s": age, "cadence_s": round(c, 1) if c else None}
    if st and not st.get("ok") and (not last or now - last > 30):
        out.update(state="failed", key=st.get("key") or "")
    elif not last:
        out["state"] = "nodata" if st and st.get("ok") and now - float(st.get("t") or now) > 120 else "waiting"
    elif grade == "interrupted":
        out["state"] = "stale"
    else:
        out["state"] = "fresh"
    return out


class Overview:
    def __init__(self, core, clock: Callable[[], float] = time.time):
        self.core = core
        self.clock = clock
        self.history: deque = deque(maxlen=HISTORY_LEN)     # [t, level rank, online, total]
        self._last_sample = 0.0
        self._cache = {}                                    # name -> (t, value)
        self.waiting = Waiting(getattr(core, "store", None))   # rows that wait on a person (lanloom/waiting.py)

    # ------------------------------------------------------------------ cache
    def _cached(self, name: str, now: float, fn: Callable, ttl: float = CACHE_S):
        hit = self._cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[name] = (now, value)
        return value

    # ------------------------------------------------------------------ health history
    def maybe_sample(self, now: float) -> None:
        """Called by the live loop every second; takes one sample every HISTORY_STEP_S."""
        if now - self._last_sample < HISTORY_STEP_S:
            return
        self._last_sample = now
        try:
            s = self.summary(now)
            if s["empty"]:
                self._last_sample = 0.0                     # nothing to judge before the first inventory
                return
            self.history.append([round(now), _rank(s["health"]["level"]), s["devices"]["online"], s["devices"]["total"]])
        except Exception:      # a sample must never stop the live loop
            pass

    # ------------------------------------------------------------------ building blocks
    def _devices(self, now: float) -> list:
        """The devices as the start page sees them (second interfaces left out); shared by the page and the rail."""
        return self._cached("devices", now, lambda: self._load_devices(now))

    def _load_devices(self, now: float) -> list:
        core = self.core
        reg = core.registry()
        try:
            members = core.identity_groups()["members"]
        except Exception:
            members = {}
        watch = getattr(getattr(core, "reports", None), "watch", None)
        wdev = watch.dev if watch is not None else {}
        down_since = watch.down_since if watch is not None else {}
        out = []
        last_scan = core._last_scan_id()                    # one query for all devices, not one per device
        for dev in core.store.devices():
            if dev["id"] in members:
                continue                                    # a second interface of a known device
            data = dev["data"]
            out.append({
                "id": dev["id"], "ip": dev["ip"], "title": dev.get("user_title") or dev["title"] or "",
                "kind": dev.get("user_kind") or dev["kind"], "hidden": bool(dev["hidden"]),
                "is_self": bool(data.get("is_self")), "is_gateway": bool(data.get("is_gateway")),
                "certainty": "sure" if dev.get("user_kind") else data.get("certainty", "unknown"),
                "first_seen": dev["first_seen"], "last_seen": dev["last_seen"], "status": core.device_status(dev, last_scan),
                "origin": reg.origin_of(dev.get("mac")), "vendor": reg.lookup(dev.get("mac")) or "",
                "was_up": wdev.get(dev["id"]) in ("up", "down", "gone"), "down_since": down_since.get(dev["id"]),
            })
        return out

    def _baseline(self, devs: list) -> float:
        first = [d["first_seen"] for d in devs if d["first_seen"]]
        return (min(first) + FIRST_SCAN_GRACE_S) if first else 0.0

    def _new(self, devs: list, now: float) -> list:
        base = max(now - NEW_WINDOW_S, self._baseline(devs))
        new = [d for d in devs if not d["is_self"] and not d["hidden"] and (d["first_seen"] or 0) > base]
        return sorted(new, key=lambda d: -(d["first_seen"] or 0))

    def _sources(self, now: float) -> list:
        core = self.core
        out = []
        if not getattr(core, "remote", False) and ("host" in core.cadence or not core.demo):   # a remote site has no host source (M1)
            out.append(dict(source_state(core, "host", now), id="host", name="source.name.host", kind=""))
        for integ in core.integrations():
            key = "int:%d" % integ["id"]
            out.append(dict(source_state(core, key, now, integ["enabled"]), id=key, name=integ["name"], kind=integ["kind"],
                            integ=integ["id"]))
        return out

    def _wan(self, now: float) -> dict:
        from .core import uplink_trace
        core = self.core
        router = core.ip_to_device.get(core.gateway_ip or "")
        rates = core.quant.rates(now)
        down = up = 0.0
        if router:
            tr = uplink_trace(router)
            down, up = rates.get((tr, 0), 0.0), rates.get((tr, 1), 0.0)
        watch = getattr(getattr(core, "reports", None), "watch", None)
        states = list((watch.wan if watch is not None else {}).values())
        peers = len(core.outside_list(now)) if hasattr(core, "outside_list") else 0
        if "down" in states:
            state = "down"
        elif "up" in states or down + up > 0 or peers:
            state = "up"
        else:
            state = "unknown"
        last_out = None
        journal = getattr(getattr(core, "reports", None), "journal", None)
        if journal is not None:
            ev = journal.last_before(now, ("wan_down",))
            if ev and now - ev["t"] < 7 * 86400:
                last_out = ev["t"]
        return {"state": state, "router": router, "down": round(down), "up": round(up), "peers": peers,
                "measured": bool(router and down + up > 0), "last_outage": last_out}

    def _paths(self, now: float) -> dict:
        # data paths are booked once a minute; the full summary is the expensive part of the page, so it is kept a minute
        return self._cached("paths", now, lambda: self._load_paths(now), PATHS_CACHE_S)

    def _load_paths(self, now: float) -> dict:
        try:
            s = self.core.paths_summary("24h")
        except Exception:
            return {"available": False}
        tot = s.get("totals") or {}
        return {"available": bool(tot.get("tx") or tot.get("rx")), "tx": tot.get("tx") or 0, "rx": tot.get("rx") or 0,
                "countries": tot.get("countries") or 0, "outside_eu_share": tot.get("outside_eu_share"),
                "top": [{"cc": c["cc"], "bytes": c["tx"] + c["rx"]} for c in (s.get("countries") or [])[:3]],
                "unusual": len(s.get("unusual") or [])}

    def _report(self) -> dict:
        rep = getattr(self.core, "reports", None)
        if rep is None:
            return {"available": False}
        try:
            rows = rep.summary_rows()
        except Exception:
            return {"available": False}
        unread = sum(1 for r in rows if not r["read"])
        latest = rows[0] if rows else None
        return {"available": True, "unread": unread, "count": len(rows),
                "latest": {k: latest[k] for k in ("day", "level", "points", "open", "read", "created")} if latest else None,
                "next_at": rep.next_at() if hasattr(rep, "next_at") else None}

    # ------------------------------------------------------------------ noteworthy
    def notes(self, now: float, devs: list = None, sources: list = None, wan: dict = None, report: dict = None,
              new: list = None) -> list:
        devs = self._devices(now) if devs is None else devs
        sources = self._sources(now) if sources is None else sources
        wan = self._wan(now) if wan is None else wan
        report = self._report() if report is None else report
        new = self._new(devs, now) if new is None else new
        out = []
        if wan["state"] == "down":
            out.append({"id": "wan", "level": "danger", "key": "ov.note.wan_down", "sub": "ov.note.wan_down.sub", "vars": {}, "link": "#/map/internet"})
        for d in devs:
            if d["hidden"] or d["status"] != "down" or not d["was_up"]:
                continue
            infra = d["kind"] in INFRA_KINDS or d["is_gateway"]
            if not infra:
                continue
            since = d["down_since"]
            out.append({"id": "dev:" + d["id"], "level": "danger" if d["is_gateway"] else "warn", "key": "ov.note.dev_offline", "human": True,
                        # without a known start (a controller reports the device as disconnected) the line says no time
                        "sub": "ov.note.dev_offline.sub" if since else "ov.note.dev_offline.sub_unknown",
                        "vars": {"name": d["title"] or d["ip"], "kind_key": "kind." + d["kind"], "age_dur": round(now - since) if since else None},
                        "link": "#/devices/" + d["id"]})
        for s in sources:
            name = s["name"]
            if s["state"] == "failed":
                out.append({"id": "src:" + s["id"], "level": "warn", "key": "ov.note.src_failed", "sub": "ov.note.src_failed.sub", "human": True,
                            "vars": {"name": name, "name_key": name if not s["kind"] else "", "reason_key": s.get("key") or ""},
                            "link": "#/settings"})
            elif s["state"] == "stale":
                out.append({"id": "src:" + s["id"], "level": "info", "key": "ov.note.src_stale", "sub": "ov.note.src_stale.sub",
                            "vars": {"name": name, "name_key": name if not s["kind"] else "", "age_dur": s["age_s"]},
                            "link": "#/settings"})
        unknown = [d for d in new if d["origin"] in ("random", "unregistered") or d["certainty"] != "sure"]
        if unknown:
            out.append({"id": "new-unknown", "level": "info", "key": "ov.note.new_unknown", "sub": "ov.note.new_unknown.sub", "human": True,
                        "vars": {"n": len(unknown), "name": unknown[0]["title"] or unknown[0]["ip"]}, "link": "#/devices?show=new"})
        latest = (report or {}).get("latest")
        if latest and not latest["read"] and latest["open"]:
            out.append({"id": "report:" + latest["day"], "level": "info", "key": "ov.note.report_open", "sub": "ov.note.report_open.sub",
                        "vars": {"n": latest["open"], "day": latest["day"]}, "link": "#/reports/" + latest["day"]})
        for fn in NOTES:
            try:
                out.extend(fn(self.core, now) or [])
            except Exception:
                pass
        return sorted(out, key=lambda n: (-_rank(n["level"]), n["id"]))

    # ------------------------------------------------------------------ summary (start page)
    def summary(self, now: float = None) -> dict:
        now = self.clock() if now is None else now
        return self._cached("summary", now, lambda: self._summary(now))

    def _summary(self, now: float) -> dict:
        devs = self._devices(now)
        shown = [d for d in devs if not d["hidden"]]
        new = self._new(devs, now)
        sources = self._sources(now)
        wan = self._wan(now)
        report = self._report()
        notes, waiting = self.waiting.split(self.notes(now, devs, sources, wan, report, new), now)
        level = notes[0]["level"] if notes else "ok"           # rows that wait on a person drown out nothing
        hh = getattr(self.core, "hosthealth", None)
        try:
            forecasts = hh.hints(now) if hh is not None else []
        except Exception:
            forecasts = []
        unsure = [d for d in shown if not d["is_self"] and d["certainty"] != "sure"]
        unsure.sort(key=lambda d: -(d["first_seen"] or 0))
        fresh = sum(1 for s in sources if s["state"] == "fresh")
        active = [s for s in sources if s["state"] != "off"]
        history = list(self.history)
        demo_hist = getattr(self.core.demo, "overview_history", None)
        if callable(demo_hist):                             # the demo shows a whole day; its own samples replace the tail
            base = demo_hist(now, HISTORY_STEP_S, HISTORY_LEN)
            history = (base[:len(base) - len(history)] + history) if history else base
        trim = lambda d: {k: d[k] for k in ("id", "ip", "title", "kind", "first_seen", "status", "origin", "vendor", "certainty")}   # noqa: E731
        return {
            "t": round(now, 3),
            "devices": {"total": len(shown), "online": sum(1 for d in shown if d["status"] == "up"),
                        "offline": sum(1 for d in shown if d["status"] == "down"), "new": len(new), "unsure": len(unsure),
                        "hidden": len(devs) - len(shown)},
            "sources": {"total": len(active), "fresh": fresh,
                        "stale": sum(1 for s in active if s["state"] in ("stale", "failed")),
                        "waiting": sum(1 for s in active if s["state"] in ("waiting", "nodata")),
                        "overall": live.typical([s["grade"] for s in active])},
            "wan": wan,
            "paths": self._paths(now),
            "report": report,
            "health": {"level": level, "notes": len(notes), "history": history, "step_s": HISTORY_STEP_S},
            "notes": notes,
            "waiting": waiting,
            "forecasts": forecasts[:MAX_LIST * 2],
            "incidents": self._incidents(now),
            "new": [trim(d) for d in new[:MAX_LIST]],
            "unsure": [trim(d) for d in unsure[:MAX_LIST]],
            "source_list": sources,
            "empty": not devs,
        }

    def _incidents(self, now: float):
        inc = getattr(self.core, "incidents", None)
        if inc is None or not self.core.availability.has_data():
            return None
        try:
            return inc.overview(now)
        except Exception:
            return None

    # ------------------------------------------------------------------ right now (live rail)
    def right_now(self, now: float = None) -> dict:
        now = self.clock() if now is None else now
        return self._cached("now", now, lambda: self._right_now(now))

    def _names(self, now: float) -> dict:
        names = {}
        for d in self._devices(now):                        # the shared device pass (no second read of the inventory)
            names[d["id"]] = {"id": d["id"], "title": "" if d["is_self"] else (d["title"] or d["ip"]), "kind": "self" if d["is_self"] else d["kind"]}
        names["internet"] = {"id": "internet", "title": "", "kind": "internet"}
        names["self"] = names.get("self") or {"id": "self", "title": "", "kind": "self"}
        return names

    def talks(self, now: float) -> list:
        core = self.core
        per = {}
        for (trace, direction), r in core.quant.rates(now).items():
            per.setdefault(trace, [0.0, 0.0])[direction] = r
        names = self._names(now)
        integ = {i["id"]: i["name"] for i in core.integrations()}
        out = []
        for trace, (r0, r1) in per.items():
            if r0 + r1 < TALK_FROM:
                continue
            kind, _, rest = trace.partition(":")
            if kind in ("f", "l", "p"):
                a, _, b = rest.partition(":")
            elif kind == "u":
                a, b = "internet", rest.partition(":")[2]
            elif kind == "q":
                iid, _, root = rest.partition(":")
                try:
                    bname = integ.get(int(iid), "")
                except ValueError:
                    bname = ""
                out.append({"trace": trace, "kind": "topic", "a": {"id": "broker:" + iid, "title": bname, "kind": "broker"},
                            "b": {"id": "topic:%s:%s" % (iid, root), "title": root, "kind": "service"}, "r0": round(r0), "r1": round(r1)})
                continue
            else:
                continue
            na = names.get(a) or self._name_of(a)
            nb = names.get(b) or self._name_of(b)
            out.append({"trace": trace, "kind": {"f": "flow", "l": "lan", "p": "lan", "u": "uplink"}[kind], "a": na, "b": nb,
                        "r0": round(r0), "r1": round(r1)})
        out.sort(key=lambda x: -(x["r0"] + x["r1"]))
        return out[:MAX_TALKS]

    def _name_of(self, dev_id: str) -> dict:
        """A device the shared pass leaves out (a second interface): look it up once."""
        dev = self.core.store.device(dev_id) if dev_id.startswith("d-") else None
        if not dev:
            return {"id": dev_id, "title": dev_id, "kind": "unknown"}
        return {"id": dev_id, "title": dev.get("user_title") or dev["title"] or dev["ip"], "kind": dev.get("user_kind") or dev["kind"]}

    def changes(self, now: float) -> list:
        core = self.core
        out = []
        journal = getattr(getattr(core, "reports", None), "journal", None)
        if journal is not None:
            for ev in journal.between(now - CHANGES_WINDOW_S, now, CHANGE_KINDS):
                out.append({"t": ev["t"], "kind": ev["kind"], "data": _public(ev["data"])})
        demo_changes = getattr(core.demo, "overview_changes", None)
        if callable(demo_changes):
            out.extend(demo_changes(core, now))
        out.sort(key=lambda e: -e["t"])
        return out[:MAX_CHANGES]

    def _right_now(self, now: float) -> dict:
        devs = self._devices(now)
        new = self._new(devs, now)
        trim = lambda d: {k: d[k] for k in ("id", "ip", "title", "kind", "first_seen", "origin", "vendor")}   # noqa: E731
        return {"t": round(now, 3), "talks": self.talks(now), "changes": self.changes(now),
                "new": [trim(d) for d in new[:MAX_LIST]], "notes": [n for n in self.notes(now, devs=devs, new=new) if not self.waiting.is_waiting(n, now)],
                "rate_unit": self.core.rate_unit()}


def _public(data: dict) -> dict:
    """Journal rows carry titles and ids only; keep the fields the rail shows."""
    keep = ("id", "title", "kind", "name", "key", "ip", "vendor", "origin", "from", "to", "version", "prev",
            "sub", "level", "zone", "was", "port", "mode", "action", "n")
    return {k: data[k] for k in keep if k in data and isinstance(data[k], (str, int, float))}
