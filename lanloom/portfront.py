"""Front view of switches and access points (read only): ports as a faceplate with link, speed, power, network, what
sits behind them, how often the link dropped or counted errors in the last 24 hours, and per access point the clients
and roaming right now.

Data comes from the neutral model v2 of a controller adapter (``equipment``, ``ports``, ``devices``) and from the
light topology events of the same adapter (ports with counters, every minute). Nothing is sent to a device.

* ``PortWatch``  - keeps link drops and error episodes per port (24 hours, in the store) and turns byte counters into
  rates and deltas (the exact counters of a cable device on its own port).
* ``RoamWatch``  - remembers which access point a wireless client was on, its signal and counts moves (24 hours, in
  memory only: who sat where and when is never stored). Names a device that stays 10 minutes at -80 dBm or weaker on
  an access point although another one exists (W2) - a hint, never an error.
* ``fronts``     - the data of the view.
"""
import threading
import time

from . import wifisignal

WINDOW_S = 24 * 3600
STORE_KEY = "portwatch"
SAVE_S = 300
YELLOW_FROM, RED_FROM = 1, 3        # events in 24 h: 0 fine, 1-2 yellow, 3 and more red
MAX_EVENTS = 64                     # per port


def level_for(events: int) -> str:
    return "bad" if events >= RED_FROM else "warn" if events >= YELLOW_FROM else "ok"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class PortWatch:
    def __init__(self, store=None):
        self.store = store
        self._lock = threading.Lock()
        self.last = {}                 # port id -> {"link", "err", "tx", "rx", "t"}
        self.rate = {}                 # port id -> (tx bytes/s, rx bytes/s, t)
        self.events = {}               # port id -> [[t, "flap" | "err"], ...]
        self._saved = 0.0
        self._dirty = False
        if store is not None:
            raw = store.get_setting(STORE_KEY, None)
            if isinstance(raw, dict):
                for pid, evs in raw.items():
                    if isinstance(pid, str) and isinstance(evs, list):
                        self.events[pid] = [[float(e[0]), str(e[1])] for e in evs
                                            if isinstance(e, list) and len(e) == 2 and _num(e[0]) is not None][-MAX_EVENTS:]

    def feed(self, ports, now: float = None) -> list:
        """One reading of the ports of a controller. Returns [(port id, tx bytes, rx bytes)]: the bytes that passed the
        port since its last reading (only where both readings carry counters and the counter did not start over)."""
        now = time.time() if now is None else now
        out = []
        with self._lock:
            for p in ports or []:
                pid = p.get("id") if isinstance(p, dict) else None
                if not isinstance(pid, str):
                    continue
                link = bool(p.get("link")) if p.get("link") is not None else None
                errs = [_num(p.get("rx_errors")), _num(p.get("tx_errors"))]
                err = sum(e for e in errs if e is not None) if any(e is not None for e in errs) else None
                tx, rx = _num(p.get("tx_bytes")), _num(p.get("rx_bytes"))
                prev = self.last.get(pid)
                if prev is not None:
                    if prev["link"] is True and link is False:
                        self._event(pid, now, "flap")
                    if prev["err"] is not None and err is not None and err > prev["err"]:
                        self._event(pid, now, "err")
                    dt = now - prev["t"]
                    if dt > 0 and None not in (tx, rx, prev["tx"], prev["rx"]) and tx >= prev["tx"] and rx >= prev["rx"]:
                        dtx, drx = tx - prev["tx"], rx - prev["rx"]
                        out.append((pid, dtx, drx))
                        self.rate[pid] = (dtx / dt, drx / dt, now)
                self.last[pid] = {"link": link, "err": err, "tx": tx, "rx": rx, "t": now}
            self._prune(now)
            self._save(now)
        return out

    def _event(self, pid: str, now: float, kind: str) -> None:
        evs = self.events.setdefault(pid, [])
        evs.append([now, kind])
        del evs[:-MAX_EVENTS]
        self._dirty = True

    def _prune(self, now: float) -> None:
        for pid in list(self.events):
            self.events[pid] = [e for e in self.events[pid] if now - e[0] <= WINDOW_S]
            if not self.events[pid]:
                del self.events[pid]
                self._dirty = True
        for pid in [k for k, v in self.last.items() if now - v["t"] > WINDOW_S]:
            self.last.pop(pid, None)
            self.rate.pop(pid, None)

    def _save(self, now: float) -> None:
        if self.store is None or not self._dirty or now - self._saved < SAVE_S:
            return
        self._saved, self._dirty = now, False
        try:
            self.store.set_setting(STORE_KEY, self.events)
        except Exception:
            pass

    def stats(self, pid: str, now: float = None) -> dict:
        """{"flaps", "errors", "events", "level", "rate": {"tx", "rx"} | None} of one port."""
        now = time.time() if now is None else now
        with self._lock:
            evs = [e for e in self.events.get(pid, []) if now - e[0] <= WINDOW_S]
            r = self.rate.get(pid)
        flaps = sum(1 for e in evs if e[1] == "flap")
        errors = sum(1 for e in evs if e[1] == "err")
        rate = {"tx": round(r[0]), "rx": round(r[1])} if r and now - r[2] < 300 else None
        return {"flaps": flaps, "errors": errors, "events": len(evs), "level": level_for(len(evs)), "rate": rate}

    def problems(self, now: float = None) -> list:
        """[(port id, flaps, errors)] of ports with at least one event in 24 hours, worst first."""
        now = time.time() if now is None else now
        rows = []
        for pid in list(self.events):
            s = self.stats(pid, now)
            if s["events"]:
                rows.append((pid, s["flaps"], s["errors"]))
        rows.sort(key=lambda r: -(r[1] + r[2]))
        return rows


class RoamWatch:
    """Which access point each wireless client sits on, its signal, and the moves between access points. Everything
    lives in memory (moves for 24 hours); nothing here reaches the store - a record of who was where is a movement profile."""

    WEAKEST = 3                       # devices named in the view of an access point

    def __init__(self):
        self._lock = threading.Lock()
        self.where = {}                # client key -> access point key
        self.moves = []                # [t, from ap, to ap]
        self.clients = {}              # access point key -> {band: n}
        self.air = {}                  # client key -> {"ap", "band", "name", "signal", "weak_since"}

    def feed(self, clients, now: float = None) -> None:
        """``clients``: [{"id", "ap", "band", "signal", "name"}] of the wireless clients now (``ap`` and ``id`` opaque
        text; ``signal`` whole dBm or None, ``name`` for the view only)."""
        now = time.time() if now is None else now
        seen, per_ap, air = {}, {}, {}
        for c in clients or []:
            if not c.get("id") or not c.get("ap"):
                continue
            seen[c["id"]] = c["ap"]
            per_ap.setdefault(c["ap"], {})
            band = c.get("band") or "?"
            per_ap[c["ap"]][band] = per_ap[c["ap"]].get(band, 0) + 1
            sig = wifisignal.clean(c.get("signal"))
            air[c["id"]] = {"ap": c["ap"], "band": band, "name": c.get("name"), "signal": sig, "weak_since": None}
        with self._lock:
            for cid, ap in seen.items():
                before = self.where.get(cid)
                if before is not None and before != ap:
                    self.moves.append([now, before, ap])
                prev = self.air.get(cid)
                a = air[cid]
                if a["signal"] is not None and a["signal"] <= wifisignal.STICK_DBM:
                    # weak without a break on the same access point; a move, a missing or a better reading starts over
                    a["weak_since"] = prev["weak_since"] if prev and prev["ap"] == ap and prev["weak_since"] is not None else now
            self.where = seen                       # a client gone for a poll starts fresh when it comes back
            self.clients = per_ap
            self.air = air
            self.moves = [m for m in self.moves if now - m[0] <= WINDOW_S][-2000:]

    def counts(self, ap: str, now: float = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            mv = [m for m in self.moves if now - m[0] <= WINDOW_S]
            return {"in": sum(1 for m in mv if m[2] == ap), "out": sum(1 for m in mv if m[1] == ap)}

    def bands(self, ap: str) -> dict:
        with self._lock:
            return dict(self.clients.get(ap) or {})

    def total(self, now: float = None, since: float = None) -> int:
        """Moves in the 24 hours up to ``now`` (or only since ``since``)."""
        now = time.time() if now is None else now
        with self._lock:
            return sum(1 for m in self.moves if now - m[0] <= WINDOW_S and (since is None or m[0] >= since))

    def signal_view(self, ap: str, others: bool, now: float = None) -> dict:
        """What the view of one access point shows about the signal of its clients now: ``bands`` {band: {"good",
        "fair", "weak"}}, ``weakest`` (the weakest few with a reading) and ``stuck`` (those that clung to this access
        point for 10 minutes at -80 dBm or weaker). ``others``: another access point exists in the site - without one
        nothing can be stuck, there is nowhere to go. Empty when no client has a reading."""
        now = time.time() if now is None else now
        with self._lock:
            mine = [dict(a) for a in self.air.values() if a["ap"] == ap and a["signal"] is not None]
        if not mine:
            return {}
        bands = {}
        for a in mine:
            row = bands.setdefault(a["band"], {"good": 0, "fair": 0, "weak": 0})
            row[wifisignal.grade(a["signal"])] += 1
        out = {"bands": bands, "weakest": [], "stuck": []}
        for a in sorted(mine, key=lambda x: x["signal"])[:self.WEAKEST]:
            out["weakest"].append({"name": a["name"] or "?", "signal": a["signal"], "band": a["band"]})
        if others:
            for a in sorted(mine, key=lambda x: x["weak_since"] or now):
                if a["weak_since"] is not None and now - a["weak_since"] >= wifisignal.STICK_S:
                    out["stuck"].append({"name": a["name"] or "?", "signal": a["signal"], "band": a["band"],
                                         "since_s": int(now - a["weak_since"])})
        return out

    def stuck(self, aps: dict, now: float = None) -> list:
        """The stuck devices of all access points in ``aps`` {access point key: name}: [{"name", "ap", "signal",
        "band", "since_s"}], longest first. The caller says which access points exist (``len(aps) >= 2``)."""
        if len(aps) < 2:
            return []
        now = time.time() if now is None else now
        with self._lock:
            rows = [dict(a) for a in self.air.values() if a["weak_since"] is not None and a["ap"] in aps
                    and now - a["weak_since"] >= wifisignal.STICK_S]
        rows.sort(key=lambda a: a["weak_since"])
        return [{"name": a["name"] or "?", "ap": aps[a["ap"]], "signal": a["signal"], "band": a["band"],
                 "since_s": int(now - a["weak_since"])} for a in rows]


def _sfp(p: dict) -> bool:
    media = str(p.get("media") or "").upper()
    return media.startswith("SFP") or "SFP" in str(p.get("name") or "").upper()


def fronts(state: dict, watch: PortWatch, roam: RoamWatch, now: float = None, behind: dict = None, ap_keys: dict = None,
           ap_total: int = None) -> list:
    """The faceplates of every switch and access point of a neutral state, router first.

    ``behind``: {equipment id: {port idx: [{"name", "kind", "wired"}]}} what the topology knows besides the clients of
    the state. ``ap_keys``: {equipment id: access point key as used by RoamWatch}. ``ap_total``: how many access points the
    site has in all (default: those of this state) - a device can only be stuck where there is another one."""
    now = time.time() if now is None else now
    behind = behind or {}
    ap_keys = ap_keys or {}
    nets = {n["id"]: n for n in state.get("networks") or []}
    eq = {e["id"]: e for e in state.get("equipment") or []}
    by_dev = {}
    for d in state.get("devices") or []:
        if d.get("online") is False:
            continue
        if d.get("wired") and d.get("parent") in eq and _num(d.get("port")) is not None:
            by_dev.setdefault(d["parent"], {}).setdefault(int(d["port"]), []).append(
                {"name": d.get("name") or "?", "kind": d.get("kind") or "client", "wired": True, "ip": d.get("ip")})
    out = []
    if ap_total is None:
        ap_total = sum(1 for e in eq.values() if (e.get("kind") == "ap" or e.get("radios")) and e.get("online") is not False)
    order = {"router": 0, "switch": 1, "ap": 2}
    for e in sorted(eq.values(), key=lambda x: (order.get(x.get("kind"), 3), x.get("name") or "")):
        ports = sorted((p for p in state.get("ports") or [] if p.get("device") == e["id"]), key=lambda p: p.get("idx") or 0)
        rows = []
        for p in ports:
            st = watch.stats(p["id"], now)
            devs = list(by_dev.get(e["id"], {}).get(p.get("idx"), []))
            for extra in (behind.get(e["id"]) or {}).get(p.get("idx"), []):
                if not any(x["name"] == extra["name"] for x in devs):
                    devs.append(extra)
            neighbor = eq.get(p.get("lldp_neighbor")) if p.get("lldp_neighbor") else None
            native = nets.get(p.get("native_network"))
            vlan = p.get("native_vlan")
            rows.append({"id": p["id"], "idx": p.get("idx"), "name": p.get("name") or str(p.get("idx")), "link": bool(p.get("link")),
                         "speed": p.get("speed"), "duplex": p.get("duplex"), "sfp": _sfp(p),
                         "poe": ({"capable": bool((p.get("poe") or {}).get("capable")), "on": bool((p.get("poe") or {}).get("on")),
                                  "watts": (p.get("poe") or {}).get("watts")} if p.get("poe") else None),
                         "vlan": vlan, "network": (native or {}).get("name"),
                         "profile": p.get("profile_name"),
                         "tagged": bool(p.get("tagged")) or p.get("tagged_mode") == "all", "tagged_mode": p.get("tagged_mode"),
                         "tagged_names": [nets[n]["name"] for n in p.get("tagged") or [] if isinstance(n, str) and n in nets][:12],
                         "poe_mode": (p.get("poe") or {}).get("mode") if isinstance(p.get("poe"), dict) else None,
                         "uplink": bool(p.get("is_uplink")), "neighbor": (neighbor or {}).get("name"),
                         "devices": devs[:6], "n_devices": len(devs), "flaps": st["flaps"], "errors": st["errors"], "events": st["events"],
                         "level": st["level"], "rate": st["rate"]})
        item = {"id": e["id"], "name": e.get("name") or "?", "kind": e.get("kind") or "other", "model": e.get("model"),
                "online": bool(e.get("online")), "ip": e.get("mgmt_ip"), "clients": (e.get("stats") or {}).get("clients"),
                "ports": rows, "radios": [], "level": "ok"}
        worst = max((r["events"] for r in rows), default=0)
        item["level"] = level_for(worst)
        if e.get("kind") == "ap" or e.get("radios"):
            key = ap_keys.get(e["id"])
            live = roam.bands(key) if key else {}
            for r in e.get("radios") or []:
                n = live.get(r.get("band")) if live else None
                item["radios"].append({"band": r.get("band"), "channel": r.get("channel"),
                                       "clients": n if n is not None else r.get("clients")})
            if key:
                item["roams"] = roam.counts(key, now)
                sig = roam.signal_view(key, ap_total >= 2 and e.get("online") is not False, now)
                if sig:
                    item["signal"] = sig
        if rows or item["radios"]:
            out.append(item)
    return out
