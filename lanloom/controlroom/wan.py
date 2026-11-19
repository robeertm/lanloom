"""Internet watch with two axes, a flap guard, a history and the failover (reserve line) state.

**Axis 1 - what the gateway says:** the ``wans`` list of a controller family (``up``, ``active``, ``role``, rates; see
``lanloom/firewall/neutral.py``) and, without one, the operational state of the uplink port a router reports over SNMP.
**Axis 2 - what is measured:** an optional probe (name lookup and/or TCP connection to targets an administrator picks;
nothing is probed until targets are set). Lanloom never trusts a claim over a measurement: when the probe is fresh, the
probe decides and a disagreeing gateway is only noted ("the gateway says down, but the internet answers").

**Flap guard:** a state change counts after 3 equal results in a row (down) or 2 (up). Four confirmed changes inside ten
minutes make the state ``flapping``; it stays so until ten quiet minutes - and writes one history line, not twenty.

**History:** a state character per 15-minute slot for 35 days (``u`` up, ``r`` up on the reserve line, ``f`` flapping,
``d`` down, ``?`` unknown, worst wins) and a log of changes (``wan_log``). Uptime is counted over the slots that have a state.
The reserve line is a ``wans`` entry with ``role: backup`` (or the one that is not primary); ``active`` says which one carries traffic.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Optional

from .levels import freshness, num

SLOT_S = 900
KEEP_DAYS = 35
DOWN_AFTER = 3                      # equal results in a row before "down" counts
UP_AFTER = 2
FLAP_CHANGES = 4
FLAP_WINDOW_S = 600.0
FLAP_QUIET_S = 600.0
PROBE_EVERY_S = 20.0
PROBE_TIMEOUT_S = 3.0
PROBE_FRESH_S = 90.0                # a probe result older than this does not count as a measurement
GATEWAY_FRESH_S = 1200.0
MAX_TARGETS = 4
UNKNOWN_AFTER_S = 300.0            # no axis has a fresh reading for this long: the state is unknown, not the old one
SETTING = "wan_probe"
# ready-made target sets an administrator can pick (no fixed host is ever probed before that)
PRESETS = (
    {"id": "resolvers", "dns": "", "tcp": ("1.1.1.1:53", "9.9.9.9:53")},
    {"id": "lookup", "dns": "example.org", "tcp": ()},
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS wan_slots (slot INTEGER PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wan_log (t REAL NOT NULL, kind TEXT NOT NULL, line TEXT NOT NULL DEFAULT '', basis TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS wan_log_t ON wan_log(t);
"""
SLOT_RANK = {"d": 5, "f": 4, "r": 3, "u": 2, "?": 1}


def clean_target(text) -> Optional[str]:
    """``host:port`` (port 1-65535) or a bare host (port 443); None for anything else."""
    s = str(text or "").strip().lower()
    if not s or len(s) > 253 or any(c.isspace() or ord(c) < 32 for c in s):
        return None
    host, _, port = s.rpartition(":") if s.count(":") == 1 else (s, "", "443")
    host = host or s
    try:
        p = int(port or 443)
    except ValueError:
        return None
    if not 1 <= p <= 65535 or not host or "/" in host or "@" in host:
        return None
    return "%s:%d" % (host, p)


def clean_name(text) -> Optional[str]:
    s = str(text or "").strip().lower().rstrip(".")
    if not s:
        return ""
    if len(s) > 253 or not all(c.isalnum() or c in "-." for c in s) or s.startswith(("-", ".")):
        return None
    return s


def clean_probe(data: dict) -> dict:
    """The probe setting from user input; raises ValueError(i18n key)."""
    data = data or {}
    name = clean_name(data.get("dns"))
    if name is None:
        raise ValueError("wan.err.probe_name")
    targets = []
    for t in list(data.get("tcp") or [])[:MAX_TARGETS + 1]:
        c = clean_target(t)
        if c is None:
            raise ValueError("wan.err.probe_target")
        if c not in targets:
            targets.append(c)
    if len(targets) > MAX_TARGETS:
        raise ValueError("wan.err.probe_many")
    return {"enabled": bool(data.get("enabled", True)) and bool(name or targets), "dns": name, "tcp": targets}


class WanWatch:
    def __init__(self, core, clock=time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self._lock = threading.RLock()
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.store.execute(stmt)
        self.probe_state = {"t": None, "ok": None, "ms": None, "dns_ok": None, "tcp_ok": None, "fails": 0}
        self._streak = {"state": None, "n": 0}
        self.confirmed: Optional[str] = None          # up | down | None (not known yet)
        self.since: Optional[float] = None
        self.changes: list = []                       # timestamps of confirmed changes
        self.flapping_since: Optional[float] = None
        self.basis = "none"
        self.disagree = "none"
        self.reserve_since: Optional[float] = None
        self._on_reserve: Optional[bool] = None
        self._last_probe_t = 0.0
        self._last_slot = None
        self._unknown_since: Optional[float] = None

    # ------------------------------------------------------------------ settings
    def probe_setting(self) -> dict:
        cur = self.store.get_setting(SETTING, None)
        if isinstance(cur, dict):
            try:
                return clean_probe(dict(cur, enabled=cur.get("enabled", True)))
            except ValueError:
                pass
        return {"enabled": False, "dns": "", "tcp": []}

    def set_probe(self, data: dict) -> dict:
        cfg = clean_probe(data)
        self.store.set_setting(SETTING, cfg)
        with self._lock:
            self.probe_state = {"t": None, "ok": None, "ms": None, "dns_ok": None, "tcp_ok": None, "fails": 0}
        return cfg

    # ------------------------------------------------------------------ the probe (the only traffic of its own)
    async def probe_once(self) -> dict:
        """One measurement against the configured targets; returns {"ok", "ms", "dns_ok", "tcp_ok"} (None = not asked)."""
        cfg = self.probe_setting()
        if not cfg["enabled"]:
            return {"ok": None}
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        dns_ok = tcp_ok = None
        if cfg["dns"]:
            try:
                await asyncio.wait_for(loop.getaddrinfo(cfg["dns"], None, type=socket.SOCK_STREAM), PROBE_TIMEOUT_S)
                dns_ok = True
            except Exception:
                dns_ok = False
        if cfg["tcp"]:
            tcp_ok = False
            for target in cfg["tcp"]:
                host, _, port = target.rpartition(":")
                try:
                    _r, w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), PROBE_TIMEOUT_S)
                    w.close()
                    tcp_ok = True
                    break
                except Exception:
                    continue
        ok = all(x for x in (dns_ok, tcp_ok) if x is not None)
        ms = round((time.monotonic() - t0) * 1000.0, 1) if ok else None
        return {"ok": ok, "ms": ms, "dns_ok": dns_ok, "tcp_ok": tcp_ok}

    def feed_probe(self, result: dict, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        if result.get("ok") is None:
            return
        with self._lock:
            ps = self.probe_state
            ps.update({"t": now, "ok": bool(result["ok"]), "ms": result.get("ms"), "dns_ok": result.get("dns_ok"), "tcp_ok": result.get("tcp_ok")})
            ps["fails"] = 0 if result["ok"] else ps["fails"] + 1

    async def probe_if_due(self) -> None:
        now = self.clock()
        if now - self._last_probe_t < PROBE_EVERY_S:
            return
        self._last_probe_t = now
        self.feed_probe(await self.probe_once(), now)

    # ------------------------------------------------------------------ axis 1: the gateway
    def gateway_lines(self, now: float) -> tuple:
        """(lines, age_s of the newest configuration with a ``wans`` list) from the controller families."""
        lines, newest = [], None
        try:
            entries = self.core.live_configs(GATEWAY_FRESH_S)
        except Exception:
            entries = []
        for e in entries:
            for w in (e.get("state") or {}).get("wans") or []:
                lines.append(self._line(w, e))
                newest = e["age_s"] if newest is None else min(newest, e["age_s"])
        if any(ln["up"] is not None for ln in lines):          # a line that only has a name adds nothing next to lines that tell their state
            lines = [ln for ln in lines if ln["up"] is not None or ln["active"] is not None or ln["rx_bps"] is not None]
        roles = [ln["role"] for ln in lines]
        if lines and "backup" not in roles and len(lines) > 1:
            primary = next((ln for ln in lines if ln["role"] == "primary"), None)
            for ln in lines:
                if ln["role"] == "single":
                    ln["role"] = "primary" if primary is None and ln is lines[0] else "backup"
        return lines, newest

    @staticmethod
    def _line(w: dict, entry: dict) -> dict:
        role = w.get("role") if w.get("role") in ("primary", "backup") else "single"
        return {"id": w["id"], "name": str(w.get("name") or "?")[:64], "kind": w.get("kind") if w.get("kind") in ("cable", "cellular") else "other",
                "role": role, "up": w.get("up") if isinstance(w.get("up"), bool) else None,
                "active": w.get("active") if isinstance(w.get("active"), bool) else None,
                "enabled": w.get("enabled") is not False, "rx_bps": num(w.get("rx_bps")), "tx_bps": num(w.get("tx_bps")),
                "latency_ms": num(w.get("latency_ms")), "source": entry.get("name") or ""}

    def _snmp_uplink(self, now: float) -> tuple:
        """(up|down|None, age_s) from the uplink port of a router read over SNMP."""
        core = self.core
        gw_ip = getattr(core, "gateway_ip", None)
        best = None
        for integ_id, ev in list((getattr(core, "snmp", None) or {}).items()):
            if gw_ip and ev.get("host") != gw_ip:
                continue
            from ..core import pick_uplink
            itf = pick_uplink(ev.get("ifaces") or [], core.store.get_setting("uplink_iface", ""))
            if itf and itf.get("oper") in ("up", "down"):
                age = max(0.0, now - float(ev.get("t") or now))
                if best is None or age < best[1]:
                    best = (itf["oper"], age)
        return best or (None, None)

    def gateway_axis(self, now: float, lines: Optional[list] = None, age: Optional[float] = None) -> dict:
        if lines is None:
            lines, age = self.gateway_lines(now)
        known = [ln for ln in lines if ln["enabled"] and ln["up"] is not None]
        if known:
            state = "up" if any(ln["up"] for ln in known) else "down"
            return {"state": state, "age_s": age, "from": "wans"}
        oper, age2 = self._snmp_uplink(now)
        if oper:
            return {"state": oper, "age_s": age2, "from": "uplink"}
        return {"state": "unknown", "age_s": None, "from": ""}

    # ------------------------------------------------------------------ the decision
    def _probe_axis(self, now: float) -> dict:
        ps, cfg = self.probe_state, self.probe_setting()
        if not cfg["enabled"]:
            return {"state": "off", "age_s": None, "latency_ms": None, "fails": 0}
        if ps["t"] is None or now - ps["t"] > PROBE_FRESH_S:
            return {"state": "unknown", "age_s": None if ps["t"] is None else round(now - ps["t"], 1), "latency_ms": None, "fails": ps["fails"]}
        return {"state": "up" if ps["ok"] else "down", "age_s": round(now - ps["t"], 1), "latency_ms": ps["ms"], "fails": ps["fails"]}

    def step(self, now: Optional[float] = None) -> dict:
        """Evaluate both axes once, apply the flap guard, write slots and log. Called every ~15 s by the control room."""
        now = self.clock() if now is None else now
        with self._lock:
            lines, age = self.gateway_lines(now)
            gw = self.gateway_axis(now, lines, age)
            pr = self._probe_axis(now)
            if pr["state"] in ("up", "down"):
                raw, basis = pr["state"], ("both" if gw["state"] == pr["state"] else "measured")
                self.disagree = "none" if gw["state"] in ("unknown", pr["state"]) else (
                    "gateway_down_measured_up" if gw["state"] == "down" else "gateway_up_measured_down")
            elif gw["state"] in ("up", "down"):
                raw, basis, self.disagree = gw["state"], "gateway", "none"
            else:
                raw, basis, self.disagree = "unknown", "none", "none"
            self.basis = basis
            self._unknown_since = (self._unknown_since or now) if raw == "unknown" else None
            self._apply(raw, now)
            self._failover(lines, now)
            self._slot(now)
            return {"raw": raw, "gateway": gw, "probe": pr, "lines": lines}

    def _apply(self, raw: str, now: float) -> None:
        if raw == "unknown":
            self._streak = {"state": None, "n": 0}
            return
        s = self._streak
        s["n"] = s["n"] + 1 if s["state"] == raw else 1
        s["state"] = raw
        need = DOWN_AFTER if raw == "down" else UP_AFTER
        if self.confirmed is None:
            self.confirmed, self.since = raw, now           # first knowledge: take it, nothing to log
            return
        if raw != self.confirmed and s["n"] >= need:
            self.confirmed, self.since = raw, now
            self.changes = [t for t in self.changes if now - t <= FLAP_WINDOW_S] + [now]
            if len(self.changes) >= FLAP_CHANGES and self.flapping_since is None:
                self.flapping_since = now
                self._log(now, "flapping", "", self.basis, "")
            elif self.flapping_since is None:
                self._log(now, raw, "", self.basis, self.disagree if self.disagree != "none" else "")
        if self.flapping_since is not None and not [t for t in self.changes if now - t <= FLAP_QUIET_S]:
            self.flapping_since = None
            self._log(now, "steady", "", self.basis, "")

    def _failover(self, lines: list, now: float) -> None:
        reserve = [ln for ln in lines if ln["role"] == "backup" and ln["enabled"]]
        if not reserve:
            self._on_reserve = None
            self.reserve_since = None
            return
        primary = [ln for ln in lines if ln["role"] == "primary" and ln["enabled"]]
        on = None
        if any(ln["active"] is True for ln in reserve):
            on = True
        elif any(ln["active"] is True for ln in primary):
            on = False
        elif primary and all(ln["up"] is False for ln in primary) and any(ln["up"] for ln in reserve):
            on = True
        elif primary and any(ln["up"] for ln in primary):
            on = False
        if on is None:
            return
        if self._on_reserve is not None and on != self._on_reserve:
            self._log(now, "failover" if on else "failback", (reserve[0]["name"] if on else primary[0]["name"] if primary else ""), self.basis, "")
        if on != self._on_reserve:
            self.reserve_since = now if on else None
        self._on_reserve = on

    def _slot(self, now: float) -> None:
        slot = int(now // SLOT_S)
        state = "?" if self.confirmed is None or self._unknown_since is not None and now - self._unknown_since > UNKNOWN_AFTER_S else "f" if self.flapping_since is not None else "d" if self.confirmed == "down" else "r" if self._on_reserve else "u"
        row = self.store.one("SELECT state FROM wan_slots WHERE slot=?", (slot,))
        if row is None or SLOT_RANK[state] > SLOT_RANK.get(row["state"], 0):
            self.store.execute("INSERT OR REPLACE INTO wan_slots(slot, state) VALUES (?,?)", (slot, state))
        if self._last_slot != slot:
            self._last_slot = slot
            self.store.execute("DELETE FROM wan_slots WHERE slot < ?", (slot - KEEP_DAYS * 86400 // SLOT_S,))
            self.store.execute("DELETE FROM wan_log WHERE t < ?", (now - KEEP_DAYS * 86400,))

    def _log(self, t: float, kind: str, line: str, basis: str, detail: str) -> None:
        self.store.execute("INSERT INTO wan_log(t, kind, line, basis, detail) VALUES (?,?,?,?,?)", (t, kind, line, basis, detail))

    def ingest_gateway_events(self, events: list) -> int:
        """Events a gateway adapter delivers (``[{t, kind: up|down|failover|failback, line}]``), added once by (t, kind, line)."""
        n = 0
        for e in events or []:
            if not isinstance(e, dict) or e.get("kind") not in ("up", "down", "failover", "failback"):
                continue
            t = num(e.get("t"))
            if t is None:
                continue
            line = str(e.get("line") or "")[:64]
            if self.store.one("SELECT t FROM wan_log WHERE t=? AND kind=? AND line=? AND basis='gateway-event'", (t, e["kind"], line)):
                continue
            self._log(t, e["kind"], line, "gateway-event", "")
            n += 1
        return n

    # ------------------------------------------------------------------ views
    def history(self, now: Optional[float] = None, hours: int = 24) -> dict:
        now = self.clock() if now is None else now
        cur = int(now // SLOT_S)
        n = hours * 3600 // SLOT_S
        rows = {r["slot"]: r["state"] for r in self.store.query("SELECT slot, state FROM wan_slots WHERE slot > ?", (cur - n,))}
        bar = "".join(rows.get(s, "?") for s in range(cur - n + 1, cur + 1))
        wk = self.store.query("SELECT state FROM wan_slots WHERE slot > ?", (cur - 7 * 86400 // SLOT_S,))
        known = [r["state"] for r in wk if r["state"] != "?"]
        up = sum(1 for s in known if s in ("u", "r"))
        events = self.store.query("SELECT t, kind, line, basis, detail FROM wan_log WHERE t >= ? ORDER BY t DESC LIMIT 40", (now - 7 * 86400,))
        return {"bar": bar, "slot_s": SLOT_S, "start_t": (cur - n + 1) * SLOT_S, "uptime_pct_7d": round(100.0 * up / len(known), 2) if known else None,
                "events": [dict(e) for e in events], "outages_7d": sum(1 for e in events if e["kind"] == "down")}

    def episodes(self, since: float, until: float) -> list:
        """Outages and flapping inside [since, until]: [{kind: down|flapping, from_t, to_t, open}]."""
        rows = self.store.query("SELECT t, kind FROM wan_log WHERE t <= ? ORDER BY t", (until,))
        out, start, kind = [], None, None
        for r in rows:
            k, t = r["kind"], r["t"]
            if k in ("down", "flapping") and start is None:
                start, kind = t, k
            elif k == "down" and kind == "flapping":
                kind = "down"
            elif k in ("up", "steady") and start is not None:
                if t >= since:
                    out.append({"kind": kind, "from_t": max(start, since), "to_t": t, "open": False})
                start = None
        if start is not None:
            out.append({"kind": kind, "from_t": max(start, since), "to_t": until, "open": True})
        return out

    def failovers_24h(self, now: float) -> int:
        row = self.store.one("SELECT COUNT(*) AS n FROM wan_log WHERE kind='failover' AND t >= ?", (now - 86400,))
        return int(row["n"]) if row else 0

    def view(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        with self._lock:
            return self._view(now)

    def _view(self, now: float) -> dict:
        lines, age = self.gateway_lines(now)
        gw = self.gateway_axis(now, lines, age)
        pr = self._probe_axis(now)
        blind = self._unknown_since is not None and now - self._unknown_since > UNKNOWN_AFTER_S
        state = ("unknown" if self.confirmed is None or blind else "flapping" if self.flapping_since is not None else self.confirmed)
        level = {"up": "ok", "down": "danger", "flapping": "warn", "unknown": "unknown"}[state]
        reserve = [ln for ln in lines if ln["role"] == "backup"]
        if state == "up" and self._on_reserve:
            level = "warn"
        cfg = self.probe_setting()
        return {"t": now, "state": state, "level": level, "basis": self.basis, "disagree": self.disagree, "since_t": self.since,
                "axes": {"gateway": dict(gw, fresh=freshness(gw["age_s"], 180, GATEWAY_FRESH_S)), "probe": pr},
                "probe": {"enabled": cfg["enabled"], "configured": bool(cfg["dns"] or cfg["tcp"]), "dns": cfg["dns"], "tcp": cfg["tcp"],
                          "presets": [{"id": p["id"], "dns": p["dns"], "tcp": list(p["tcp"])} for p in PRESETS]},
                "lines": lines,
                "failover": {"has_reserve": bool(reserve), "on_reserve": bool(self._on_reserve), "since_t": self.reserve_since,
                             "switches_24h": self.failovers_24h(now)},
                "flapping_since": self.flapping_since, "history": self.history(now)}
