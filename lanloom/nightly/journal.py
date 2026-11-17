"""Event journal for the nightly report: what changed, and when.

The live state of Lanloom (presence, source status, uplink counters) lives in memory. The report needs what happened
overnight, so a gentle watcher compares the live state once a minute and writes a row for every change:

    wan_down / wan_up          {name}                internet uplink of the router (SNMP oper status)
    wan_switch                 {from, to}            the active uplink changed (adapters that know it)
    dev_new                    {id, title, ip, kind, vendor, origin}  a device seen for the first time
                                                     (not a second interface of a known device)
    dev_offline / dev_back     {id, title}           a known device stopped / started answering
    dev_gone                   {id, title, since}    offline for a day (once per absence)
    src_fail / src_ok          {id, name, key}       a connection (integration) failed / recovered
    app_start / app_stop       {version, prev?}      Lanloom itself (maintenance, never a fault)

Rows carry titles, never secrets. Nothing here talks to the network.
"""
import json
import logging
import time
from typing import Callable, Optional

log = logging.getLogger("lanloom")

INFRA_KINDS = ("router", "switch", "ap", "nas", "server", "hub", "broker")
GONE_AFTER_S = 86400.0          # offline this long = "not seen for a day" (reported once)
START_GRACE_S = 300.0           # after a start, presence needs a few rounds before "offline" means anything
JOURNAL_MIN_DAYS = 35


class Journal:
    def __init__(self, store, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock
        self.listeners: list = []          # (kind, data, t) callbacks, e.g. the notification rules

    def add(self, kind: str, data: Optional[dict] = None, t: Optional[float] = None) -> None:
        self.store.execute("INSERT INTO events(t, kind, data) VALUES(?,?,?)",
                           (float(self.clock() if t is None else t), kind, json.dumps(data or {}, separators=(",", ":"))))
        for fn in list(self.listeners):
            try:
                fn(kind, dict(data or {}), float(self.clock() if t is None else t))
            except Exception:
                log.exception("journal listener failed (%s)", kind)

    def between(self, since: float, until: float, kinds: Optional[tuple] = None) -> list:
        rows = self.store.query("SELECT t, kind, data FROM events WHERE t > ? AND t <= ? ORDER BY t, id", (since, until))
        out = []
        for r in rows:
            if kinds and r["kind"] not in kinds:
                continue
            try:
                r["data"] = json.loads(r["data"])
            except ValueError:
                r["data"] = {}
            out.append(r)
        return out

    def last_before(self, t: float, kinds: tuple, match: Optional[Callable[[dict], bool]] = None) -> Optional[dict]:
        marks = ",".join("?" * len(kinds))
        for r in self.store.query("SELECT t, kind, data FROM events WHERE t <= ? AND kind IN (%s) ORDER BY t DESC, id DESC LIMIT 400"
                                  % marks, (t,) + tuple(kinds)):
            try:
                r["data"] = json.loads(r["data"])
            except ValueError:
                continue
            if match is None or match(r["data"]):
                return r
        return None

    def any(self, kinds: tuple) -> bool:
        marks = ",".join("?" * len(kinds))
        return self.store.one("SELECT 1 AS x FROM events WHERE kind IN (%s) LIMIT 1" % marks, tuple(kinds)) is not None

    def prune(self, keep_days: int) -> None:
        days = max(JOURNAL_MIN_DAYS, int(keep_days) + 5)
        self.store.execute("DELETE FROM events WHERE t < ?", (self.clock() - days * 86400,))


def intervals(events: list, down_kind: str, up_kind: str, since: float, until: float, down_at_start: bool) -> list:
    """[(start, end, open)] of down periods inside (since, until] from a sorted list of down/up events."""
    out, start = [], (since if down_at_start else None)
    for ev in events:
        if ev["kind"] == down_kind and start is None:
            start = ev["t"]
        elif ev["kind"] == up_kind and start is not None:
            out.append((start, ev["t"], False))
            start = None
    if start is not None:
        out.append((start, until, True))
    return out


class Watch:
    """Compares the live state with the last observation and journals every change (once a minute)."""

    def __init__(self, core, journal: Journal, clock: Callable[[], float] = time.time):
        self.core = core
        self.journal = journal
        self.clock = clock
        self.started = clock()
        saved = core.store.get_setting("report_watch", {}) or {}
        self.dev = dict(saved.get("dev") or {})       # device id -> "up" | "down" | "down0" (never seen up) | "gone"
        self.down_since = dict(saved.get("down_since") or {})
        self.src = dict(saved.get("src") or {})       # source key -> [ok, key]
        self.wan = dict(saved.get("wan") or {})       # integration id -> "up" | "down"
        self.known = set(saved.get("known") or [])
        self.seeded = bool(saved.get("seeded"))

    def save(self) -> None:
        self.core.store.set_setting("report_watch", {"dev": self.dev, "down_since": self.down_since, "src": self.src,
                                                     "wan": self.wan, "known": sorted(self.known), "seeded": True})

    def observe(self, now: Optional[float] = None) -> int:
        """One comparison round; returns the number of journalled events."""
        now = self.clock() if now is None else now
        n = self._devices(now) + self._sources(now) + self._wan(now)
        self.seeded = True
        self.save()
        return n

    # ------------------------------------------------------------------ devices
    def _devices(self, now: float) -> int:
        core, n = self.core, 0
        presence_ready = bool(core.cadence.get("presence") and core.cadence["presence"].last) or bool(core.demo)
        settled = presence_ready and now - self.started >= START_GRACE_S
        ids = set()
        groups = core.identity_groups() if hasattr(core, "identity_groups") else {"members": {}}
        for dev in core.store.devices():
            did = dev["id"]
            ids.add(did)
            title = dev.get("user_title") or dev["title"] or dev["ip"]
            if did not in self.known:
                self.known.add(did)
                merged = groups["members"].get(did)
                second_iface = bool(merged) and merged["primary"] in self.known   # not a new device
                if self.seeded and not dev["data"].get("is_self") and not second_iface:
                    reg = core.registry()
                    self.journal.add("dev_new", {"id": did, "title": title, "ip": dev["ip"],
                                                 "kind": dev.get("user_kind") or dev["kind"],
                                                 "vendor": reg.lookup(dev.get("mac")) or "",
                                                 "origin": reg.origin_of(dev.get("mac"))}, now)
                    n += 1
            if not settled or dev["hidden"]:
                continue
            status = core.device_status(dev)
            if status == "unknown":
                continue
            before = self.dev.get(did)
            kind = dev.get("user_kind") or dev["kind"]
            if status == "up":
                if before in ("down", "gone"):
                    self.journal.add("dev_back", {"id": did, "title": title, "kind": kind}, now)
                    n += 1
                self.dev[did] = "up"
                self.down_since.pop(did, None)
            else:
                if before == "up":
                    self.journal.add("dev_offline", {"id": did, "title": title, "kind": kind}, now)
                    self.down_since[did] = now
                    n += 1
                elif before is None:
                    self.down_since.setdefault(did, now)     # first look: offline, but nobody saw it go
                if before != "gone":
                    self.dev[did] = "down0" if before in (None, "down0") else "down"
                since = self.down_since.get(did, now)
                if before in ("down", "down0") and now - since >= GONE_AFTER_S:
                    self.journal.add("dev_gone", {"id": did, "title": title, "kind": kind, "since": since}, now)
                    self.dev[did] = "gone"
                    n += 1
        for did in list(self.dev):
            if did not in ids:
                self.dev.pop(did, None)
                self.down_since.pop(did, None)
        self.known &= ids
        return n

    # ------------------------------------------------------------------ connections (integrations)
    def _sources(self, now: float) -> int:
        n = 0
        names = {"int:%d" % i["id"]: i["name"] for i in self.core.integrations()}
        for key, st in list(self.core.source_status.items()):
            if key not in names:
                continue
            ok, reason = bool(st.get("ok")), st.get("key") or ""
            before = self.src.get(key)
            if before is None:
                self.src[key] = [ok, reason]
                if not ok and self.seeded:
                    self.journal.add("src_fail", {"id": key, "name": names[key], "key": reason}, now)
                    n += 1
                continue
            if before[0] and not ok:
                self.journal.add("src_fail", {"id": key, "name": names[key], "key": reason}, now)
                n += 1
            elif not before[0] and ok:
                self.journal.add("src_ok", {"id": key, "name": names[key]}, now)
                n += 1
            elif not ok and reason != before[1]:
                self.journal.add("src_fail", {"id": key, "name": names[key], "key": reason}, now)
                n += 1
            self.src[key] = [ok, reason]
        for key in list(self.src):
            if key not in names:
                self.src.pop(key, None)
        return n

    # ------------------------------------------------------------------ internet uplink (router counters)
    def _wan(self, now: float) -> int:
        from ..core import pick_uplink
        core, n = self.core, 0
        router = core.gateway_ip
        for integ_id, ev in list(core.snmp.items()):
            if not router or (ev.get("host") or "") != router:
                cfg = self._integ_cfg(integ_id)
                if (cfg.get("host") or "") != router:
                    continue
            if now - float(ev.get("t") or 0) > 300:
                continue                                   # stale counters say nothing about the line
            cfg = self._integ_cfg(integ_id)
            itf = pick_uplink(ev.get("ifaces") or [], cfg.get("uplink") or core.store.get_setting("uplink_iface", ""))
            if not itf or itf.get("oper") not in ("up", "down"):
                continue
            key, state = str(integ_id), itf["oper"]
            before = self.wan.get(key)
            if before and before != state:
                self.journal.add("wan_down" if state == "down" else "wan_up", {"name": itf.get("name") or ""}, now)
                n += 1
            self.wan[key] = state
        return n

    def _integ_cfg(self, integ_id) -> dict:
        row = self.core.store.one("SELECT config FROM integrations WHERE id=?", (integ_id,)) if integ_id else None
        try:
            return json.loads(row["config"]) if row else {}
        except ValueError:
            return {}
