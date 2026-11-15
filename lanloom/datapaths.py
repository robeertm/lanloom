"""Data paths: where the devices of the network send data on the internet, per country, offline.

Input: flows with a local device and a peer address beyond the LAN (today the host's own TCP flows; gateway flow
sources can feed the same ``book`` call later). Every peer is put in exactly one bucket:
  * a country code from the bundled geo data (``geo.py``),
  * OWN: own networks and tunnels: the carrier-grade NAT range used by overlay VPNs, special/reserved ranges of the
    geo data and every network listed in the setting ``paths_own_networks`` (for example the public address of a
    second site or a VPN provider). Own traffic is shown, but it is never "unassigned" and never "outside the EU",
  * UNASSIGNED: a valid address the geo data has no country for (should be rare).
Without loaded geo data nothing is booked at all: a start-up race must not turn real traffic into "unassigned".

Stored are only country, device id and bytes per hour (49 hours) and per day (31 days), plus the first and last day a
device talked to a country. No peer addresses, host names or visited sites are kept.

The share "outside the EU" refers only to attributable traffic (countries, not own and not unassigned); without
attributable traffic it is ``None`` ("no data"), never 0 %.

Unusual behaviour, calm and without alarms (never creates a firewall rule):
  * new_country: a device talks to a country for the first time in 30 days, at least 1 MB in 24 hours, and only after
    the device has 3 days of history (no flood of "new" right after installation),
  * heavy: upload in the last 24 hours at least 3x the typical day (median of up to 14 previous days, at least 3 of
    them with data) and at least 200 MB,
  * offline_net: a device in a network listed as "should not reach the internet" (``paths_no_internet``) talks to
    the internet anyway.
"""
import ipaddress
import statistics
import threading
import time
from typing import Callable, Optional

OWN = "~own"
UNASSIGNED = "~unassigned"
EU = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())
WINDOWS = {"24h": ("hour", 24), "7d": ("day", 7), "30d": ("day", 30)}
KEEP_HOURS = 49
KEEP_DAYS = 31
FLUSH_EVERY = 60.0
CACHE_S = 30.0
NEW_MIN_BYTES = 1_000_000
NEW_WARMUP_S = 3 * 86400
NEW_FORGET_S = 30 * 86400
HEAVY_FACTOR = 3.0
HEAVY_MIN_BYTES = 200_000_000
HEAVY_MIN_DAYS = 3
HEAVY_LOOKBACK = 14
MAX_MATRIX = 4000
MAX_NETS = 64
_TUNNEL_NETS = [ipaddress.ip_network("100.64.0.0/10")]       # carrier-grade NAT space, used by overlay VPNs

SCHEMA = """
CREATE TABLE IF NOT EXISTS paths_hour (hour INTEGER NOT NULL, device TEXT NOT NULL, cc TEXT NOT NULL,
    tx INTEGER NOT NULL DEFAULT 0, rx INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (hour, device, cc));
CREATE TABLE IF NOT EXISTS paths_day (day INTEGER NOT NULL, device TEXT NOT NULL, cc TEXT NOT NULL,
    tx INTEGER NOT NULL DEFAULT 0, rx INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, device, cc));
CREATE TABLE IF NOT EXISTS paths_seen (device TEXT NOT NULL, cc TEXT NOT NULL, first REAL NOT NULL, last REAL NOT NULL,
    PRIMARY KEY (device, cc));
"""


def parse_networks(values) -> Optional[list]:
    """List of CIDR strings -> normalised list, or None if anything is invalid."""
    if not isinstance(values, list) or len(values) > MAX_NETS:
        return None
    out = []
    for v in values:
        if not isinstance(v, str) or not v.strip() or len(v) > 64:
            return None
        try:
            net = ipaddress.ip_network(v.strip(), strict=False)
        except ValueError:
            return None
        if str(net) not in out:
            out.append(str(net))
    return out


def _nets(values) -> list:
    out = []
    for v in values or ():
        try:
            out.append(ipaddress.ip_network(v, strict=False))
        except (ValueError, TypeError):
            pass
    return out


def _in(addr, nets) -> bool:
    return any(addr.version == n.version and addr in n for n in nets)


class DataPaths:
    def __init__(self, store, geo, settings: Callable[[], dict], clock: Callable[[], float] = time.time):
        self.store = store
        self.geo = geo
        self.settings = settings
        self.clock = clock
        self.pending: dict = {}            # (hour, device, cc) -> [tx, rx]
        self.seen_pending: dict = {}       # (device, cc) -> last ts
        self.last_flush = clock()
        self._lock = threading.Lock()
        self._cache: dict = {}             # window -> (ts, summary)
        self._nets_key = None
        self._own_nets: list = []
        self._offline_nets: list = []
        self.unbooked = 0                  # bytes dropped because the geo data was not loaded yet
        with store.transaction() as tx:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    tx._db.execute(stmt)

    # ------------------------------------------------------------------ classification
    def _refresh_nets(self) -> None:
        s = self.settings()
        key = (tuple(s.get("paths_own_networks") or ()), tuple(s.get("paths_no_internet") or ()))
        if key != self._nets_key:
            self._nets_key = key
            self._own_nets = _nets(key[0])
            self._offline_nets = _nets(key[1])

    def classify(self, peer: str) -> Optional[str]:
        """Bucket of a peer beyond the LAN: country code, OWN or UNASSIGNED; None without geo data or for no address."""
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return None
        if self.geo.db is None:
            return None
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        self._refresh_nets()
        if _in(addr, _TUNNEL_NETS) or _in(addr, self._own_nets):
            return OWN
        cc = self.geo.country(str(addr))
        if cc is None:
            return UNASSIGNED
        return OWN if cc == "ZZ" else cc

    # ------------------------------------------------------------------ booking
    def book(self, device: str, peer: str, tx, rx, now: Optional[float] = None) -> bool:
        tx, rx = max(0, int(tx or 0)), max(0, int(rx or 0))
        if not (tx or rx) or not device:
            return False
        cat = self.classify(peer)
        if cat is None:
            if self.geo.db is None:
                self.unbooked += tx + rx
            return False
        self.book_bucket(device, cat, tx, rx, now)
        return True

    def book_bucket(self, device: str, cat: str, tx: int, rx: int, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        key = (int(now // 3600), device, cat)
        with self._lock:
            acc = self.pending.setdefault(key, [0, 0])
            acc[0] += int(tx)
            acc[1] += int(rx)
            if not cat.startswith("~"):
                sk = (device, cat)
                self.seen_pending[sk] = max(self.seen_pending.get(sk, 0.0), now)

    def maybe_flush(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        if now - self.last_flush >= FLUSH_EVERY:
            self.flush(now)

    def flush(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            pending, self.pending = self.pending, {}
            seen, self.seen_pending = self.seen_pending, {}
            self.last_flush = now
        if not pending and not seen:
            return
        with self.store.transaction() as st:
            db = st._db
            for (hour, device, cat), (tx, rx) in pending.items():
                db.execute("INSERT INTO paths_hour(hour, device, cc, tx, rx) VALUES(?,?,?,?,?) ON CONFLICT(hour, device, cc)"
                           " DO UPDATE SET tx=tx+excluded.tx, rx=rx+excluded.rx", (hour, device, cat, tx, rx))
                db.execute("INSERT INTO paths_day(day, device, cc, tx, rx) VALUES(?,?,?,?,?) ON CONFLICT(day, device, cc)"
                           " DO UPDATE SET tx=tx+excluded.tx, rx=rx+excluded.rx", (hour // 24, device, cat, tx, rx))
            for (device, cc), ts in seen.items():
                row = db.execute("SELECT first, last FROM paths_seen WHERE device=? AND cc=?", (device, cc)).fetchone()
                if row is None:
                    db.execute("INSERT INTO paths_seen(device, cc, first, last) VALUES(?,?,?,?)", (device, cc, ts, ts))
                elif ts - row["last"] > NEW_FORGET_S:       # silent for 30 days: counts as new again
                    db.execute("UPDATE paths_seen SET first=?, last=? WHERE device=? AND cc=?", (ts, ts, device, cc))
                elif ts > row["last"]:
                    db.execute("UPDATE paths_seen SET last=? WHERE device=? AND cc=?", (ts, device, cc))
            hour = int(now // 3600)
            db.execute("DELETE FROM paths_hour WHERE hour < ?", (hour - KEEP_HOURS,))
            db.execute("DELETE FROM paths_day WHERE day < ?", (hour // 24 - KEEP_DAYS,))
        self._cache.clear()

    def forget_device(self, device: str) -> None:
        with self._lock:
            self.pending = {k: v for k, v in self.pending.items() if k[1] != device}
            self.seen_pending = {k: v for k, v in self.seen_pending.items() if k[0] != device}
        for table in ("paths_hour", "paths_day", "paths_seen"):
            self.store.execute("DELETE FROM %s WHERE device=?" % table, (device,))
        self._cache.clear()

    # ------------------------------------------------------------------ reading
    def _rows(self, window: str, now: float) -> list:
        unit, n = WINDOWS[window]
        hour = int(now // 3600)
        if unit == "hour":
            sql, since = "SELECT device, cc, SUM(tx) AS tx, SUM(rx) AS rx FROM paths_hour WHERE hour > ? GROUP BY device, cc", hour - n
        else:
            sql, since = "SELECT device, cc, SUM(tx) AS tx, SUM(rx) AS rx FROM paths_day WHERE day > ? GROUP BY device, cc", hour // 24 - n
        return self.store.query(sql, (since,))

    def summary(self, window: str, devices: dict, now: Optional[float] = None) -> dict:
        """Everything the view needs for one window. ``devices``: id -> {"title", "ip", "kind"}."""
        if window not in WINDOWS:
            window = "24h"
        now = self.clock() if now is None else now
        self.flush(now)
        hit = self._cache.get(window)
        if hit and now - hit[0] < CACHE_S and hit[2] == len(devices):
            return hit[1]
        rows = self._rows(window, now)
        self._refresh_nets()
        countries, devs, matrix = {}, {}, []
        tot = {"tx": 0, "rx": 0, "own_tx": 0, "own_rx": 0, "unassigned_tx": 0, "unassigned_rx": 0, "outside_eu": 0}
        for r in rows:
            tx, rx, cc, dev = int(r["tx"] or 0), int(r["rx"] or 0), r["cc"], r["device"]
            if cc == OWN:
                tot["own_tx"] += tx; tot["own_rx"] += rx
                continue
            if cc == UNASSIGNED:
                tot["unassigned_tx"] += tx; tot["unassigned_rx"] += rx
            else:
                tot["tx"] += tx; tot["rx"] += rx
                if cc not in EU:
                    tot["outside_eu"] += tx + rx
                c = countries.setdefault(cc, {"cc": cc, "eu": cc in EU, "tx": 0, "rx": 0, "devices": 0})
                c["tx"] += tx; c["rx"] += rx; c["devices"] += 1
            d = devs.setdefault(dev, {"id": dev, "tx": 0, "rx": 0, "countries": 0})
            d["tx"] += tx; d["rx"] += rx
            if cc != UNASSIGNED:
                d["countries"] += 1
            matrix.append([dev, cc, tx, rx])
        attributable = tot["tx"] + tot["rx"]
        for d in devs.values():
            info = devices.get(d["id"]) or {}
            d["title"] = info.get("title") or ""
            d["ip"] = info.get("ip")
            d["kind"] = info.get("kind") or ""
            d["self"] = bool(info.get("self"))
        matrix.sort(key=lambda m: -(m[2] + m[3]))
        out = {
            "window": window, "t": round(now, 1),
            "geo": self.geo.info(),
            "home": (self.settings().get("home_country") or ""),
            "totals": {
                "tx": tot["tx"], "rx": tot["rx"], "attributable": attributable,
                "own": tot["own_tx"] + tot["own_rx"], "unassigned": tot["unassigned_tx"] + tot["unassigned_rx"],
                "outside_eu": tot["outside_eu"],
                "outside_eu_share": (tot["outside_eu"] / attributable) if attributable > 0 else None,
                "countries": len(countries), "devices": len(devs),
            },
            "countries": sorted(countries.values(), key=lambda c: -(c["tx"] + c["rx"])),
            "devices": sorted(devs.values(), key=lambda d: -(d["tx"] + d["rx"])),
            "matrix": matrix[:MAX_MATRIX],
            "unusual": self.unusual(now, devices, rows if window == "24h" else self._rows("24h", now)),
        }
        self._cache[window] = (now, out, len(devices))
        return out

    def unusual(self, now: float, devices: dict, rows24: list) -> list:
        out = []
        per_dev_cc = {(r["device"], r["cc"]): int(r["tx"] or 0) + int(r["rx"] or 0) for r in rows24}
        # new country
        seen = self.store.query("SELECT device, cc, first FROM paths_seen")
        start = {}
        for s in seen:
            start[s["device"]] = min(start.get(s["device"], s["first"]), s["first"])
        for s in seen:
            if now - s["first"] > 86400 or s["first"] - start[s["device"]] < NEW_WARMUP_S:
                continue
            b = per_dev_cc.get((s["device"], s["cc"]), 0)
            if b >= NEW_MIN_BYTES:
                out.append({"kind": "new_country", "device": s["device"], "cc": s["cc"], "bytes": b, "since": s["first"]})
        # heavy upload
        today = int(now // 86400)
        up24 = {}
        for r in rows24:
            up24[r["device"]] = up24.get(r["device"], 0) + int(r["tx"] or 0)
        hist = {}
        for r in self.store.query("SELECT device, day, SUM(tx) AS tx FROM paths_day WHERE day >= ? AND day < ? GROUP BY device, day",
                                  (today - HEAVY_LOOKBACK, today)):
            hist.setdefault(r["device"], []).append(int(r["tx"] or 0))
        for dev, up in up24.items():
            days = hist.get(dev, [])
            if len(days) < HEAVY_MIN_DAYS or up < HEAVY_MIN_BYTES:
                continue
            typical = statistics.median(days)
            if up >= HEAVY_FACTOR * typical:
                out.append({"kind": "heavy", "device": dev, "bytes": up, "typical": int(typical)})
        # network without internet
        if self._offline_nets:
            talk = {}
            for r in rows24:
                if r["cc"] != OWN:
                    talk[r["device"]] = talk.get(r["device"], 0) + int(r["tx"] or 0) + int(r["rx"] or 0)
            for dev, b in talk.items():
                ip = (devices.get(dev) or {}).get("ip")
                try:
                    addr = ipaddress.ip_address(ip) if ip else None
                except ValueError:
                    addr = None
                net = next((n for n in self._offline_nets if addr is not None and addr.version == n.version and addr in n), None)
                if net is not None and b > 0:
                    out.append({"kind": "offline_net", "device": dev, "bytes": b, "network": str(net)})
        order = {"offline_net": 0, "new_country": 1, "heavy": 2}
        out.sort(key=lambda u: (order[u["kind"]], -u["bytes"]))
        return out[:50]
