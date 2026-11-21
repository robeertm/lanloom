"""Data paths: where the devices of the network send data on the internet, per country, offline.

Input: flows with a local device and a peer address beyond the LAN (today the host's own TCP flows; gateway flow
sources can feed the same ``book`` call later). Every peer is put in exactly one bucket:
  * a country code from the bundled geo data (``geo.py``),
  * OWN: own networks and tunnels: the carrier-grade NAT range used by overlay VPNs, special/reserved ranges of the
    geo data and every network listed in the setting ``paths_own_networks`` (for example the public address of a
    second site or a VPN provider). Own traffic is shown, but it is never "unassigned" and never "outside the EU",
  * ANYCAST: well-known anycast networks (``ANYCAST_NETS``). The geo data only knows the country a block is registered
    in, but these networks answer from the nearest of many locations worldwide, so booking them under the registration
    country would invent traffic to a far-away country. Anycast is shown as its own entry, never as a country and never
    in the share "outside the EU". A network listed in ``paths_own_networks`` wins (counts as own),
  * UNASSIGNED: a valid address the geo data has no country for (should be rare).
Without loaded geo data nothing is booked at all: a start-up race must not turn real traffic into "unassigned".

Stored are only country, device id and bytes per hour (49 hours) and per day (31 days), plus the first and last day a
device talked to a country. No peer addresses, host names or visited sites are kept.

Cities and network operators (an option, off by default, ``geocity.py``): with the option on, the bytes of a booking are
also kept per country, city (name and region of the city centre of the PEER, never of the user), AS number and device in
``paths_city_hour``/``paths_city_day``; the names, coordinates of the city centre and operator names live in two small
tables (``paths_place``, ``paths_operator``) so the view works without the big file. At most ``MAX_PLACES`` cities and
``MAX_OPERATORS`` operators per country and day, everything beyond is condensed into "other". Anycast, own and unassigned
traffic never gets a city or an operator. The country tables stay the one truth of the sums: what has no city is shown
as such, not dropped.

The share "outside the EU" refers only to attributable traffic (countries, not own, not anycast, not unassigned); without
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

from . import infraroute

OWN = "~own"
ANYCAST = "~anycast"
UNASSIGNED = "~unassigned"
EU = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())
PERIODS = {"24h": ("hour", 24), "7d": ("day", 7), "30d": ("day", 30)}
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
MAX_PAIRS = 300                 # conversations between two devices the map draws
MAX_PAIR_KEYS = 20000           # pending pair sums held in memory between two flushes
MAX_NETS = 64
MAX_PLACES = 250                # cities per country and day; more are condensed into OTHER_CITY
MAX_OPERATORS = 250             # operators per country and day; more are condensed into OTHER_ASN
MAX_DETAIL_KEYS = 50000         # pending detail sums held in memory between two flushes
MAX_MAP_POINTS = 60             # bubbles the country map draws
MAX_OPERATOR_LIST = 500
OTHER_CITY = "~other"
OTHER_ASN = -1
_TUNNEL_NETS = [ipaddress.ip_network("100.64.0.0/10")]       # carrier-grade NAT space, used by overlay VPNs
# Well-known anycast networks, answered from the nearest location (snapshot of November 2026, renewed with a release):
# the published address ranges of the largest anycast CDN (its web, DNS and time services), its public resolver
# networks (announced by the same network, not in its published list) and the resolver networks of two other public
# DNS services. Only networks that are anycast as a whole belong here, never a provider's whole address space.
ANYCAST_NETS = tuple(ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29",
    "2c0f:f248::/32",
    "1.1.1.0/24", "1.0.0.0/24",                                             # resolver of the same network
    "8.8.8.0/24", "8.8.4.0/24", "2001:4860:4860::/48",                      # public DNS resolver
    "9.9.9.0/24", "149.112.112.0/24", "2620:fe::/48",                       # public DNS resolver
))

SCHEMA = """
CREATE TABLE IF NOT EXISTS paths_hour (hour INTEGER NOT NULL, device TEXT NOT NULL, cc TEXT NOT NULL,
    tx INTEGER NOT NULL DEFAULT 0, rx INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (hour, device, cc));
CREATE TABLE IF NOT EXISTS paths_day (day INTEGER NOT NULL, device TEXT NOT NULL, cc TEXT NOT NULL,
    tx INTEGER NOT NULL DEFAULT 0, rx INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, device, cc));
CREATE TABLE IF NOT EXISTS paths_seen (device TEXT NOT NULL, cc TEXT NOT NULL, first REAL NOT NULL, last REAL NOT NULL,
    PRIMARY KEY (device, cc));
CREATE TABLE IF NOT EXISTS paths_pair_hour (hour INTEGER NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL,
    ab INTEGER NOT NULL DEFAULT 0, ba INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (hour, a, b));
CREATE TABLE IF NOT EXISTS paths_pair_day (day INTEGER NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL,
    ab INTEGER NOT NULL DEFAULT 0, ba INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, a, b));
CREATE TABLE IF NOT EXISTS paths_city_hour (hour INTEGER NOT NULL, device TEXT NOT NULL, cc TEXT NOT NULL,
    city TEXT NOT NULL, asn INTEGER NOT NULL, tx INTEGER NOT NULL DEFAULT 0, rx INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, device, cc, city, asn));
CREATE TABLE IF NOT EXISTS paths_city_day (day INTEGER NOT NULL, device TEXT NOT NULL, cc TEXT NOT NULL,
    city TEXT NOT NULL, asn INTEGER NOT NULL, tx INTEGER NOT NULL DEFAULT 0, rx INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, device, cc, city, asn));
CREATE TABLE IF NOT EXISTS paths_place (cc TEXT NOT NULL, city TEXT NOT NULL, name TEXT NOT NULL, region TEXT NOT NULL DEFAULT '',
    lat REAL NOT NULL, lon REAL NOT NULL, PRIMARY KEY (cc, city));
CREATE TABLE IF NOT EXISTS paths_operator (asn INTEGER PRIMARY KEY, name TEXT NOT NULL);
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


def ip_of_id(device: str) -> Optional[str]:
    """The address of an address-based device id ("d-ip-192-168-0-28" -> "192.168.0.28"), else None."""
    if not isinstance(device, str) or not device.startswith("d-ip-"):
        return None
    raw = device[5:]
    for cand in (raw.replace("-", "."), raw.replace("-", ":")):
        try:
            return str(ipaddress.ip_address(cand))
        except ValueError:
            continue
    return None


def _with_sat(entry: dict) -> dict:
    """Operator entry {asn, name, bytes, ...} plus ``sat`` ("leo" or "geo") when it is a satellite network (AS number table or the
    neutral words in the name); the field is absent otherwise."""
    s = infraroute.satellite(entry.get("asn"), entry.get("name"))
    if s:
        entry["sat"] = s
    return entry


class DataPaths:
    def __init__(self, store, geo, settings: Callable[[], dict], clock: Callable[[], float] = time.time):
        self.store = store
        self.geo = geo
        self.settings = settings
        self.clock = clock
        self.pending: dict = {}            # (hour, device, cc) -> [tx, rx]
        self.pair_pending: dict = {}       # (hour, a, b) with a < b -> [bytes a to b, bytes b to a]
        self._pair_cache: dict = {}        # window -> (ts, rows)
        self.seen_pending: dict = {}       # (device, cc) -> last ts
        self.detail_pending: dict = {}     # (hour, device, cc, city, asn) -> [tx, rx]
        self.place_pending: dict = {}      # (cc, city) -> (name, region, lat, lon)
        self.operator_pending: dict = {}   # asn -> name
        self._caps: dict = {}              # (day, cc) -> [cities, operators] already booked that day
        self._country_cache: dict = {}     # (cc, window) -> (ts, view)
        self.last_flush = clock()
        self._lock = threading.Lock()
        self._cache: dict = {}             # window -> (ts, summary)
        self._nets_key = None
        self._own_nets: list = []
        self._offline_nets: list = []
        self.unbooked = 0                  # bytes dropped because the geo data was not loaded yet
        self.keep_hours = KEEP_HOURS       # retention (Settings > Storage, see storage.py)
        self.keep_days = KEEP_DAYS
        self.version = 0                   # change channel: rises with every flush that wrote data
        self.route_sink = None             # network routes (routewatch.py): remembers destinations in memory, only with that option on
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
        """Bucket of a peer beyond the LAN: country code, OWN, ANYCAST or UNASSIGNED; None without geo data or for no address."""
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
        if _in(addr, ANYCAST_NETS):
            return ANYCAST
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
        hit = None
        if not cat.startswith("~"):
            hit = self._book_detail(device, peer, cat, tx, rx, now)
        sink = self.route_sink
        if sink is not None and (cat == ANYCAST or not cat.startswith("~")) and sink.wants():
            sink.note(peer, cat, hit[1] if hit else 0, hit[0] if hit else "", tx + rx, now)
        return True

    def _book_detail(self, device: str, peer: str, cc: str, tx: int, rx: int, now: Optional[float]) -> Optional[tuple]:
        """City and operator of the peer, with the option on and a file loaded; the address is looked up and dropped."""
        lookup = getattr(self.geo, "detail", None)          # a geo source without city data (tests, the demo) books none
        if lookup is None:
            return None
        try:
            hit = lookup(str(ipaddress.ip_address(peer)), cc)
        except ValueError:
            return None
        if hit is None:
            return None
        city, asn, place, operator = hit
        self.book_detail_bucket(device, cc, city, asn, tx, rx, now, place, operator)
        return hit

    def book_detail_bucket(self, device: str, cc: str, city: str, asn: int, tx: int, rx: int, now: Optional[float] = None,
                           place: Optional[dict] = None, operator: Optional[str] = None) -> None:
        """One sum for a country, city (key "name|region" or ""), AS number and device; the caps keep the tables small."""
        now = self.clock() if now is None else now
        hour = int(now // 3600)
        with self._lock:
            caps = self._caps.get((hour // 24, cc))
            if caps is None:
                caps = self._load_caps(hour // 24, cc)
            if city and city not in caps[0]:
                if len(caps[0]) >= MAX_PLACES:
                    city = OTHER_CITY
                else:
                    caps[0].add(city)
            if asn and asn not in caps[1]:
                if len(caps[1]) >= MAX_OPERATORS:
                    asn = OTHER_ASN
                else:
                    caps[1].add(asn)
            key = (hour, device, cc, city, asn)
            acc = self.detail_pending.get(key)
            if acc is None:
                if len(self.detail_pending) >= MAX_DETAIL_KEYS:
                    key = (hour, device, cc, OTHER_CITY, OTHER_ASN)
                acc = self.detail_pending.setdefault(key, [0, 0])
            acc[0] += int(tx)
            acc[1] += int(rx)
            if place and city and city != OTHER_CITY:
                self.place_pending[(cc, city)] = (place["name"], place["region"], place["lat"], place["lon"])
            if operator and asn > 0:
                self.operator_pending[asn] = operator

    def _load_caps(self, day: int, cc: str) -> list:
        """Cities and operators of a country already stored for a day (after a restart the caps continue where they were)."""
        rows = self.store.query("SELECT DISTINCT city, asn FROM paths_city_day WHERE day=? AND cc=?", (day, cc))
        caps = [{r["city"] for r in rows if r["city"] and r["city"] != OTHER_CITY}, {r["asn"] for r in rows if r["asn"] > 0}]
        if len(self._caps) > 2000:
            self._caps = {}
        self._caps[(day, cc)] = caps
        return caps

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

    def book_pair(self, src: str, dst: str, size, now: Optional[float] = None) -> bool:
        """A conversation between two known devices of the LAN (a gateway flow whose both ends are local).

        Only the two device ids and the bytes per direction are kept; the addresses, ports and protocols are not."""
        size = max(0, int(size or 0))
        if not size or not src or not dst or src == dst:
            return False
        a, b = (src, dst) if src < dst else (dst, src)
        now = self.clock() if now is None else now
        key = (int(now // 3600), a, b)
        with self._lock:
            acc = self.pair_pending.get(key)
            if acc is None:
                if len(self.pair_pending) >= MAX_PAIR_KEYS:
                    return False
                acc = self.pair_pending[key] = [0, 0]
            acc[0 if src == a else 1] += size
        return True

    def maybe_flush(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        if now - self.last_flush >= FLUSH_EVERY:
            self.flush(now)

    def flush(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            pending, self.pending = self.pending, {}
            seen, self.seen_pending = self.seen_pending, {}
            pairs, self.pair_pending = self.pair_pending, {}
            detail, self.detail_pending = self.detail_pending, {}
            places, self.place_pending = self.place_pending, {}
            operators, self.operator_pending = self.operator_pending, {}
            self.last_flush = now
        if not pending and not seen and not pairs and not detail:
            return
        with self.store.transaction() as st:
            db = st._db
            for (hour, device, cat), (tx, rx) in pending.items():
                db.execute("INSERT INTO paths_hour(hour, device, cc, tx, rx) VALUES(?,?,?,?,?) ON CONFLICT(hour, device, cc)"
                           " DO UPDATE SET tx=tx+excluded.tx, rx=rx+excluded.rx", (hour, device, cat, tx, rx))
                db.execute("INSERT INTO paths_day(day, device, cc, tx, rx) VALUES(?,?,?,?,?) ON CONFLICT(day, device, cc)"
                           " DO UPDATE SET tx=tx+excluded.tx, rx=rx+excluded.rx", (hour // 24, device, cat, tx, rx))
            for (hour, device, cc, city, asn), (tx, rx) in detail.items():
                db.execute("INSERT INTO paths_city_hour(hour, device, cc, city, asn, tx, rx) VALUES(?,?,?,?,?,?,?) ON CONFLICT(hour, device, cc, city, asn)"
                           " DO UPDATE SET tx=tx+excluded.tx, rx=rx+excluded.rx", (hour, device, cc, city, asn, tx, rx))
                db.execute("INSERT INTO paths_city_day(day, device, cc, city, asn, tx, rx) VALUES(?,?,?,?,?,?,?) ON CONFLICT(day, device, cc, city, asn)"
                           " DO UPDATE SET tx=tx+excluded.tx, rx=rx+excluded.rx", (hour // 24, device, cc, city, asn, tx, rx))
            for (cc, city), (name, region, lat, lon) in places.items():
                db.execute("INSERT OR REPLACE INTO paths_place(cc, city, name, region, lat, lon) VALUES(?,?,?,?,?,?)", (cc, city, name, region, lat, lon))
            for asn, name in operators.items():
                db.execute("INSERT OR REPLACE INTO paths_operator(asn, name) VALUES(?,?)", (asn, name))
            for (hour, a, b), (ab, ba) in pairs.items():
                db.execute("INSERT INTO paths_pair_hour(hour, a, b, ab, ba) VALUES(?,?,?,?,?) ON CONFLICT(hour, a, b)"
                           " DO UPDATE SET ab=ab+excluded.ab, ba=ba+excluded.ba", (hour, a, b, ab, ba))
                db.execute("INSERT INTO paths_pair_day(day, a, b, ab, ba) VALUES(?,?,?,?,?) ON CONFLICT(day, a, b)"
                           " DO UPDATE SET ab=ab+excluded.ab, ba=ba+excluded.ba", (hour // 24, a, b, ab, ba))
            for (device, cc), ts in seen.items():
                row = db.execute("SELECT first, last FROM paths_seen WHERE device=? AND cc=?", (device, cc)).fetchone()
                if row is None:
                    db.execute("INSERT INTO paths_seen(device, cc, first, last) VALUES(?,?,?,?)", (device, cc, ts, ts))
                elif ts - row["last"] > NEW_FORGET_S:       # silent for 30 days: counts as new again
                    db.execute("UPDATE paths_seen SET first=?, last=? WHERE device=? AND cc=?", (ts, ts, device, cc))
                elif ts > row["last"]:
                    db.execute("UPDATE paths_seen SET last=? WHERE device=? AND cc=?", (ts, device, cc))
            hour = int(now // 3600)
            db.execute("DELETE FROM paths_hour WHERE hour < ?", (hour - self.keep_hours,))
            db.execute("DELETE FROM paths_day WHERE day < ?", (hour // 24 - self.keep_days,))
            db.execute("DELETE FROM paths_pair_hour WHERE hour < ?", (hour - self.keep_hours,))
            db.execute("DELETE FROM paths_pair_day WHERE day < ?", (hour // 24 - self.keep_days,))
            self.prune_detail(db, hour, self.keep_hours, self.keep_days)
        self._cache.clear()
        self._pair_cache.clear()
        self._country_cache.clear()
        self.version += 1

    @staticmethod
    def prune_detail(db, hour: int, keep_hours: int, keep_days: int) -> int:
        """Retention of the detail tables, and the names nothing refers to any more."""
        n = db.execute("DELETE FROM paths_city_hour WHERE hour < ?", (hour - keep_hours,)).rowcount
        n += db.execute("DELETE FROM paths_city_day WHERE day < ?", (hour // 24 - keep_days,)).rowcount
        db.execute("DELETE FROM paths_place WHERE NOT EXISTS (SELECT 1 FROM paths_city_day d WHERE d.cc = paths_place.cc AND d.city = paths_place.city)"
                   " AND NOT EXISTS (SELECT 1 FROM paths_city_hour h WHERE h.cc = paths_place.cc AND h.city = paths_place.city)")
        db.execute("DELETE FROM paths_operator WHERE NOT EXISTS (SELECT 1 FROM paths_city_day d WHERE d.asn = paths_operator.asn)"
                   " AND NOT EXISTS (SELECT 1 FROM paths_city_hour h WHERE h.asn = paths_operator.asn)")
        return n

    def clear_detail(self) -> None:
        """Delete everything stored about cities and operators (the country figures stay)."""
        with self._lock:
            self.detail_pending, self.place_pending, self.operator_pending, self._caps = {}, {}, {}, {}
        with self.store.transaction() as st:
            for t in ("paths_city_hour", "paths_city_day", "paths_place", "paths_operator"):
                st._db.execute("DELETE FROM %s" % t)
        self._country_cache.clear()
        self.version += 1

    def forget_device(self, device: str) -> None:
        with self._lock:
            self.pending = {k: v for k, v in self.pending.items() if k[1] != device}
            self.seen_pending = {k: v for k, v in self.seen_pending.items() if k[0] != device}
            self.pair_pending = {k: v for k, v in self.pair_pending.items() if device not in k[1:]}
            self.detail_pending = {k: v for k, v in self.detail_pending.items() if k[1] != device}
        for table in ("paths_hour", "paths_day", "paths_seen", "paths_city_hour", "paths_city_day"):
            self.store.execute("DELETE FROM %s WHERE device=?" % table, (device,))
        for table in ("paths_pair_hour", "paths_pair_day"):
            self.store.execute("DELETE FROM %s WHERE a=? OR b=?" % table, (device, device))
        self._cache.clear()
        self._pair_cache.clear()
        self._country_cache.clear()

    def rename_device(self, old: str, new: str) -> int:
        """Book everything stored under ``old`` onto ``new`` (a device that moved from an address id to its hardware address,
        or the "self" placeholder that became the host's own entry). Sums are added where ``new`` already has a row, a
        conversation of the two ids with each other is dropped. Idempotent: a second call finds nothing. Returns the rows moved."""
        if not old or not new or old == new:
            return 0
        with self._lock:
            def rekey(table: dict, pos: int) -> None:
                for k in [k for k in table if k[pos] == old]:
                    acc = table.pop(k)
                    nk = k[:pos] + (new,) + k[pos + 1:]
                    cur = table.setdefault(nk, [0, 0])
                    cur[0] += acc[0]
                    cur[1] += acc[1]
            rekey(self.pending, 1)
            rekey(self.detail_pending, 1)
            for k in [k for k in self.seen_pending if k[0] == old]:
                ts = self.seen_pending.pop(k)
                nk = (new, k[1])
                self.seen_pending[nk] = max(self.seen_pending.get(nk, 0.0), ts)
            for k in [k for k in self.pair_pending if old in k[1:]]:
                ab, ba = self.pair_pending.pop(k)
                self._repair(self.pair_pending, k[0], k[1], k[2], ab, ba, old, new)
        moved = 0
        with self.store.transaction() as st:
            db = st._db
            for table, key in (("paths_hour", "hour"), ("paths_day", "day")):
                moved += db.execute("INSERT INTO %s(%s, device, cc, tx, rx) SELECT %s, ?, cc, tx, rx FROM %s WHERE device=?"
                                    " ON CONFLICT(%s, device, cc) DO UPDATE SET tx=tx+excluded.tx, rx=rx+excluded.rx"
                                    % (table, key, key, table, key), (new, old)).rowcount
                db.execute("DELETE FROM %s WHERE device=?" % table, (old,))
            for table, key in (("paths_city_hour", "hour"), ("paths_city_day", "day")):
                moved += db.execute("INSERT INTO %s(%s, device, cc, city, asn, tx, rx) SELECT %s, ?, cc, city, asn, tx, rx FROM %s"
                                    " WHERE device=? ON CONFLICT(%s, device, cc, city, asn) DO UPDATE SET tx=tx+excluded.tx,"
                                    " rx=rx+excluded.rx" % (table, key, key, table, key), (new, old)).rowcount
                db.execute("DELETE FROM %s WHERE device=?" % table, (old,))
            moved += db.execute("INSERT INTO paths_seen(device, cc, first, last) SELECT ?, cc, first, last FROM paths_seen"
                                " WHERE device=? ON CONFLICT(device, cc) DO UPDATE SET first=MIN(first, excluded.first),"
                                " last=MAX(last, excluded.last)", (new, old)).rowcount
            db.execute("DELETE FROM paths_seen WHERE device=?", (old,))
            for table, key in (("paths_pair_hour", "hour"), ("paths_pair_day", "day")):
                rows = db.execute("SELECT %s AS k, a, b, ab, ba FROM %s WHERE a=? OR b=?" % (key, table), (old, old)).fetchall()
                db.execute("DELETE FROM %s WHERE a=? OR b=?" % table, (old, old))
                sums: dict = {}
                for r in rows:
                    self._repair(sums, r["k"], r["a"], r["b"], int(r["ab"]), int(r["ba"]), old, new)
                for (k, a, b), (ab, ba) in sums.items():
                    db.execute("INSERT INTO %s(%s, a, b, ab, ba) VALUES(?,?,?,?,?) ON CONFLICT(%s, a, b) DO UPDATE SET"
                               " ab=ab+excluded.ab, ba=ba+excluded.ba" % (table, key, key), (k, a, b, ab, ba))
                    moved += 1
        self._cache.clear()
        self._pair_cache.clear()
        self._country_cache.clear()
        if moved:
            self.version += 1
        return moved

    @staticmethod
    def _repair(target: dict, k, a: str, b: str, ab: int, ba: int, old: str, new: str) -> None:
        """Add one conversation sum (a < b) to ``target`` with ``old`` replaced by ``new``; the order of the pair and with
        it the direction of the two sums follow the new ids. A conversation of ``new`` with itself is dropped."""
        a2, b2 = (new if a == old else a), (new if b == old else b)
        if a2 == b2:
            return
        if a2 > b2:
            a2, b2, ab, ba = b2, a2, ba, ab
        cur = target.setdefault((k, a2, b2), [0, 0])
        cur[0] += ab
        cur[1] += ba

    def stale_devices(self, known) -> list:
        """Device ids with stored data paths that no device has any more (``known``: the ids of the inventory)."""
        known = set(known)
        rows = self.store.query("SELECT device FROM paths_day UNION SELECT device FROM paths_hour UNION SELECT device FROM paths_seen")
        with self._lock:
            pend = {k[1] for k in self.pending}
        return sorted(({r["device"] for r in rows} | pend) - known)

    def snapshot_device(self, device: str, limit: int = 5000) -> dict:
        """The stored rows of one device id (a copy kept before they are booked onto another id)."""
        out = {}
        for table in ("paths_hour", "paths_day", "paths_seen", "paths_city_hour", "paths_city_day"):
            out[table] = [list(r.values()) for r in self.store.query("SELECT * FROM %s WHERE device=? LIMIT ?" % table, (device, limit))]
        for table in ("paths_pair_hour", "paths_pair_day"):
            out[table] = [list(r.values()) for r in self.store.query("SELECT * FROM %s WHERE a=? OR b=? LIMIT ?" % table,
                                                                     (device, device, limit))]
        return out

    # ------------------------------------------------------------------ reading
    def _rows(self, window: str, now: float) -> list:
        unit, n = PERIODS[window]
        hour = int(now // 3600)
        if unit == "hour":
            sql, since = "SELECT device, cc, SUM(tx) AS tx, SUM(rx) AS rx FROM paths_hour WHERE hour > ? GROUP BY device, cc", hour - n
        else:
            sql, since = "SELECT device, cc, SUM(tx) AS tx, SUM(rx) AS rx FROM paths_day WHERE day > ? GROUP BY device, cc", hour // 24 - n
        return self.store.query(sql, (since,))

    def summary(self, window: str, devices: dict, now: Optional[float] = None) -> dict:
        """Everything the view needs for one window. ``devices``: id -> {"title", "ip", "kind"}."""
        if window not in PERIODS:
            window = "24h"
        now = self.clock() if now is None else now
        self.flush(now)
        dev_key = hash(tuple(sorted((k, v.get("title"), v.get("ip")) for k, v in devices.items())))
        hit = self._cache.get(window)
        if hit and now - hit[0] < CACHE_S and hit[2] == dev_key:      # a renamed device shows at once
            return hit[1]
        rows = self._rows(window, now)
        self._refresh_nets()
        countries, devs, matrix = {}, {}, []
        tot = {"tx": 0, "rx": 0, "own_tx": 0, "own_rx": 0, "unassigned_tx": 0, "unassigned_rx": 0, "outside_eu": 0,
               "anycast_tx": 0, "anycast_rx": 0}
        anycast_devs = set()
        for r in rows:
            tx, rx, cc, dev = int(r["tx"] or 0), int(r["rx"] or 0), r["cc"], r["device"]
            if cc == OWN:
                tot["own_tx"] += tx; tot["own_rx"] += rx
                continue
            if cc == UNASSIGNED:
                tot["unassigned_tx"] += tx; tot["unassigned_rx"] += rx
            elif cc == ANYCAST:
                tot["anycast_tx"] += tx; tot["anycast_rx"] += rx
                anycast_devs.add(dev)
            else:
                tot["tx"] += tx; tot["rx"] += rx
                if cc not in EU:
                    tot["outside_eu"] += tx + rx
                c = countries.setdefault(cc, {"cc": cc, "eu": cc in EU, "tx": 0, "rx": 0, "devices": 0})
                c["tx"] += tx; c["rx"] += rx; c["devices"] += 1
            d = devs.setdefault(dev, {"id": dev, "tx": 0, "rx": 0, "countries": 0})
            d["tx"] += tx; d["rx"] += rx
            if not cc.startswith("~"):
                d["countries"] += 1
            matrix.append([dev, cc, tx, rx])
        attributable = tot["tx"] + tot["rx"]
        for d in devs.values():
            info = devices.get(d["id"]) or {}
            d["title"] = info.get("title") or ""
            d["ip"] = info.get("ip") or (None if info else ip_of_id(d["id"]))
            d["kind"] = info.get("kind") or ""
            d["self"] = bool(info.get("self"))
            if not info:
                d["gone"] = True          # no device has this id any more: the view never shows the raw id
        matrix.sort(key=lambda m: -(m[2] + m[3]))
        out = {
            "window": window, "t": round(now, 1),
            "geo": self.geo.info(),
            "home": (self.settings().get("home_country") or ""),
            "totals": {
                "tx": tot["tx"], "rx": tot["rx"], "attributable": attributable,
                "own": tot["own_tx"] + tot["own_rx"], "unassigned": tot["unassigned_tx"] + tot["unassigned_rx"],
                "anycast": tot["anycast_tx"] + tot["anycast_rx"], "anycast_tx": tot["anycast_tx"], "anycast_rx": tot["anycast_rx"],
                "anycast_devices": len(anycast_devs),
                "outside_eu": tot["outside_eu"],
                "outside_eu_share": (tot["outside_eu"] / attributable) if attributable > 0 else None,
                "countries": len(countries), "devices": len(devs),
            },
            "countries": sorted(countries.values(), key=lambda c: -(c["tx"] + c["rx"])),
            "devices": sorted(devs.values(), key=lambda d: -(d["tx"] + d["rx"])),
            "matrix": matrix[:MAX_MATRIX],
            "unusual": self.unusual(now, devices, rows if window == "24h" else self._rows("24h", now)),
        }
        self._cache[window] = (now, out, dev_key)
        return out

    def _pair_rows(self, window: str, now: float) -> list:
        unit, n = PERIODS[window]
        hour = int(now // 3600)
        if unit == "hour":
            sql, since = "SELECT a, b, SUM(ab) AS ab, SUM(ba) AS ba FROM paths_pair_hour WHERE hour > ? GROUP BY a, b", hour - n
        else:
            sql, since = "SELECT a, b, SUM(ab) AS ab, SUM(ba) AS ba FROM paths_pair_day WHERE day > ? GROUP BY a, b", hour // 24 - n
        return self.store.query(sql, (since,))

    def pairs(self, window: str, now: Optional[float] = None) -> list:
        """Conversations between two devices of the LAN in one window: [{a, b, ab, ba}] by bytes, most first."""
        if window not in PERIODS:
            window = "24h"
        now = self.clock() if now is None else now
        self.flush(now)
        hit = self._pair_cache.get(window)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
        rows = [{"a": r["a"], "b": r["b"], "ab": int(r["ab"] or 0), "ba": int(r["ba"] or 0)} for r in self._pair_rows(window, now)]
        rows = [r for r in rows if r["ab"] + r["ba"] > 0]
        rows.sort(key=lambda r: -(r["ab"] + r["ba"]))
        self._pair_cache[window] = (now, rows)
        return rows

    def device_view(self, device: str, window: str, devices: dict, now: Optional[float] = None) -> dict:
        """Where one device talks to: the internet by country plus the devices of the LAN it talks to.

        ``device`` may be ``internet``: then the answer lists the devices that talk to the internet, with their sums.
        ``devices``: id -> {"title", "ip", "kind"} for the titles."""
        if window not in PERIODS:
            window = "24h"
        now = self.clock() if now is None else now
        s = self.summary(window, devices, now)
        def meta(i):
            info = devices.get(i)
            if not info:          # no device has this id any more: an address if the id carries one, never the raw id
                return {"title": "", "kind": "", "self": False, "ip": ip_of_id(i), "gone": True}
            return {"title": info.get("title") or "", "kind": info.get("kind") or "", "self": bool(info.get("self"))}
        out = {"window": window, "t": round(now, 1), "device": device, "geo": s["geo"], "home": s["home"]}
        if device == "internet":
            devs = [{"id": d["id"], "tx": d["tx"], "rx": d["rx"], "countries": d["countries"], **meta(d["id"])} for d in s["devices"]]
            out["devices"] = devs[:MAX_PAIRS]
            out["totals"] = {"tx": sum(d["tx"] for d in devs), "rx": sum(d["rx"] for d in devs), "devices": len(devs)}
            return out
        own = unassigned = anycast = None
        countries = []
        for r in self._rows(window, now):
            dev, cc, tx, rx = r["device"], r["cc"], int(r["tx"] or 0), int(r["rx"] or 0)
            if dev != device:
                continue
            if cc == OWN:
                own = {"tx": tx, "rx": rx}
            elif cc == UNASSIGNED:
                unassigned = {"tx": tx, "rx": rx}
            elif cc == ANYCAST:
                anycast = {"tx": tx, "rx": rx}
            else:
                countries.append({"cc": cc, "eu": cc in EU, "tx": tx, "rx": rx})
        countries.sort(key=lambda c: -(c["tx"] + c["rx"]))
        lan = []
        for p in self.pairs(window, now):
            if device not in (p["a"], p["b"]):
                continue
            other = p["b"] if p["a"] == device else p["a"]
            sent, got = (p["ab"], p["ba"]) if p["a"] == device else (p["ba"], p["ab"])
            lan.append({"id": other, "tx": sent, "rx": got, **meta(other)})
        out.update({"countries": countries[:40], "own": own, "anycast": anycast, "unassigned": unassigned, "lan": lan[:40]})
        return out

    def country_view(self, cc: str, window: str, devices: dict, now: Optional[float] = None, route_places: Optional[dict] = None) -> dict:
        """The detail sheet of one country: figures, history, cities, operators, devices and targets.

        Sum rule: the figures of the header, the bubbles of the map and the list of devices all add up to the same
        number, the bytes of the country in the window (``totals.bytes``, from the country tables). What has no city is
        not dropped: it is drawn as an approximate bubble at the largest known city of the same operator in this
        country, or at the middle of the country (``lat``/``lon`` empty). With network routes measured (DW2,
        ``route_places``: AS number -> place of the destination network, 0 = traffic without an operator) that traffic is
        drawn at the measured place instead (still approximate, ``via`` = "route"); the sum stays the same. Nothing here
        carries an address."""
        cc = (cc or "").strip().upper()[:2]
        if window not in PERIODS:
            window = "24h"
        if len(cc) != 2 or not cc.isalpha():
            raise KeyError(cc)
        now = self.clock() if now is None else now
        self.flush(now)
        key = (cc, window)
        dev_key = hash((tuple(sorted((k, v.get("title")) for k, v in devices.items())),
                        tuple(sorted((a, p.get("lat"), p.get("lon")) for a, p in (route_places or {}).items() if isinstance(p, dict)))))
        hit = self._country_cache.get(key)
        if hit and now - hit[0] < CACHE_S and hit[2] == dev_key:
            return hit[1]
        unit, n = PERIODS[window]
        hour = int(now // 3600)
        if unit == "hour":
            tab, ctab, tcol, since, step = "paths_hour", "paths_city_hour", "hour", hour - n, 3600
        else:
            tab, ctab, tcol, since, step = "paths_day", "paths_city_day", "day", hour // 24 - n, 86400
        q = self.store.query
        rows = q("SELECT device, SUM(tx) AS tx, SUM(rx) AS rx FROM %s WHERE cc=? AND %s > ? GROUP BY device" % (tab, tcol), (cc, since))
        drows = q("SELECT device, city, asn, SUM(tx) AS tx, SUM(rx) AS rx FROM %s WHERE cc=? AND %s > ? GROUP BY device, city, asn" % (ctab, tcol), (cc, since))
        series_c = {r["t"]: (int(r["tx"] or 0), int(r["rx"] or 0)) for r in q(
            "SELECT %s AS t, SUM(tx) AS tx, SUM(rx) AS rx FROM %s WHERE cc=? AND %s > ? GROUP BY t" % (tcol, tab, tcol), (cc, since))}
        series_all = {r["t"]: int(r["b"] or 0) for r in q(
            "SELECT %s AS t, SUM(tx + rx) AS b FROM %s WHERE cc NOT LIKE '~%%' AND %s > ? GROUP BY t" % (tcol, tab, tcol), (since,))}
        everything = sum(series_all.values())
        tx_total = sum(int(r["tx"] or 0) for r in rows)
        rx_total = sum(int(r["rx"] or 0) for r in rows)
        total = tx_total + rx_total
        places = {r["city"]: r for r in q("SELECT city, name, region, lat, lon FROM paths_place WHERE cc=?", (cc,))}
        ops = {r["asn"]: r["name"] for r in q("SELECT asn, name FROM paths_operator")}

        by_city, by_op, by_dev, pairs = {}, {}, {}, {}
        detail_total = 0
        for r in drows:
            b = int(r["tx"] or 0) + int(r["rx"] or 0)
            if b <= 0:
                continue
            detail_total += b
            dev, city, asn = r["device"], r["city"], r["asn"]
            if city and city != OTHER_CITY:
                c = by_city.setdefault(city, {"bytes": 0, "devices": set(), "ops": {}})
                c["bytes"] += b; c["devices"].add(dev); c["ops"][asn] = c["ops"].get(asn, 0) + b
            o = by_op.setdefault(asn, {"bytes": 0, "devices": set(), "cities": {}})
            o["bytes"] += b; o["devices"].add(dev)
            if city and city != OTHER_CITY:
                o["cities"][city] = o["cities"].get(city, 0) + b
            d = by_dev.setdefault(dev, {"ops": {}, "cities": {}})
            d["ops"][asn] = d["ops"].get(asn, 0) + b
            d["cities"][city] = d["cities"].get(city, 0) + b
            p = pairs.setdefault((asn, city), {"bytes": 0, "devices": set()})
            p["bytes"] += b; p["devices"].add(dev)
        detail_total = min(detail_total, total) if total else detail_total
        no_detail = max(0, total - detail_total)

        def opname(asn):
            return ops.get(asn) or ("AS%d" % asn if asn > 0 else "")

        def cityinfo(k):
            pl = places.get(k)
            return {"key": k, "name": pl["name"], "region": pl["region"], "lat": pl["lat"], "lon": pl["lon"]} if pl else None

        # ---- cities (known ones only) and operators
        cities = []
        for k, c in sorted(by_city.items(), key=lambda x: (-x[1]["bytes"], x[0])):
            info = cityinfo(k)
            if info is None:
                continue
            cities.append(dict(info, bytes=c["bytes"], share=(c["bytes"] / total) if total else 0.0, devices=len(c["devices"]),
                               operators=[_with_sat({"asn": a, "name": opname(a), "bytes": b}) for a, b in
                                          sorted(c["ops"].items(), key=lambda x: -x[1])[:4] if a > 0]))
        operators = []
        for asn, o in sorted(by_op.items(), key=lambda x: (-x[1]["bytes"], x[0])):
            if asn == 0:
                continue
            operators.append(_with_sat({"asn": asn, "name": opname(asn) if asn > 0 else "", "other": asn < 0, "bytes": o["bytes"],
                              "share": (o["bytes"] / total) if total else 0.0, "devices": len(o["devices"]),
                              "cities": [{"name": (places.get(k) or {"name": ""})["name"], "bytes": b} for k, b in
                                         sorted(o["cities"].items(), key=lambda x: -x[1])[:4] if k in places]}))
        n_operators = sum(1 for o in operators if not o["other"])
        operators = operators[:MAX_OPERATOR_LIST]

        # ---- bubbles of the map: they add up to the country total
        best_city = {}
        for asn, o in by_op.items():
            if asn > 0 and o["cities"]:
                best_city[asn] = max(o["cities"].items(), key=lambda x: (x[1], x[0]))[0]
        known = {k: c["bytes"] for k, c in by_city.items() if k in places}
        approx = {}
        for k, c in by_city.items():
            if k not in places:
                approx[None] = approx.get(None, 0) + c["bytes"]
        rp = {int(a): p for a, p in (route_places or {}).items() if isinstance(p, dict) and p.get("lat") is not None and p.get("lon") is not None}
        for (asn, city), p in pairs.items():
            if city and city != OTHER_CITY:
                continue
            if city == "" and asn in rp:                      # a measured route knows where this operator's destinations are
                pos = "route:%d" % asn
            else:
                pos = best_city.get(asn) if (asn > 0 and city == "") else None
                if pos is not None and pos not in places:
                    pos = None
            approx[pos] = approx.get(pos, 0) + p["bytes"]
        if no_detail:
            pos = "route:0" if 0 in rp else None             # without operator data: the measured place of the country's traffic
            approx[pos] = approx.get(pos, 0) + no_detail
        points = [{"key": k, "approx": False, "bytes": b, **{x: places[k][x] for x in ("name", "region", "lat", "lon")}}
                  for k, b in known.items()]
        def approx_point(k, b):
            if k and k.startswith("route:"):
                rpl = rp[int(k[6:])]
                return {"key": k, "approx": True, "bytes": b, "via": "route", "name": rpl.get("name") or "", "region": "",
                        "lat": rpl["lat"], "lon": rpl["lon"]}
            return {"key": k, "approx": True, "bytes": b,
                    **({x: places[k][x] for x in ("name", "region", "lat", "lon")} if k else {"name": "", "region": "", "lat": None, "lon": None})}
        points += [approx_point(k, b) for k, b in approx.items() if b > 0]
        points.sort(key=lambda p: (-p["bytes"], p["key"] or "", p["approx"]))
        if len(points) > MAX_MAP_POINTS:                # keep the sum: the small ones are merged into the middle of the country
            head, tail = points[:MAX_MAP_POINTS - 1], points[MAX_MAP_POINTS - 1:]
            rest = sum(p["bytes"] for p in tail)
            mid = next((p for p in head if p["key"] is None and p["approx"]), None)
            if mid:
                mid["bytes"] += rest
            else:
                head.append({"key": None, "approx": True, "bytes": rest, "name": "", "region": "", "lat": None, "lon": None})
            points = sorted(head, key=lambda p: (-p["bytes"], p["key"] or "", p["approx"]))
        no_city = total - sum(known.values()) if total else 0

        # ---- devices and targets
        devs = []
        for r in sorted(rows, key=lambda r: -(int(r["tx"] or 0) + int(r["rx"] or 0))):
            tx, rx = int(r["tx"] or 0), int(r["rx"] or 0)
            if tx + rx <= 0:
                continue
            info = devices.get(r["device"]) or {}
            d = by_dev.get(r["device"], {"ops": {}, "cities": {}})
            devs.append({"id": r["device"], "title": info.get("title") or "", "kind": info.get("kind") or "", "self": bool(info.get("self")),
                         "tx": tx, "rx": rx, "bytes": tx + rx,
                         "operators": [_with_sat({"asn": a, "name": opname(a), "bytes": b}) for a, b in sorted(d["ops"].items(), key=lambda x: -x[1])[:3] if a > 0],
                         "cities": [places[k]["name"] for k, _ in sorted(d["cities"].items(), key=lambda x: -x[1])[:3] if k in places]})
        targets = [_with_sat({"asn": a, "name": opname(a), "city": (places.get(c) or {"name": ""})["name"], "bytes": p["bytes"], "devices": len(p["devices"])})
                   for (a, c), p in sorted(pairs.items(), key=lambda x: (-x[1]["bytes"], x[0][0], x[0][1]))[:24]]

        # ---- history
        t = (hour - n + 1) * 3600 if unit == "hour" else (hour // 24 - n + 1) * 86400
        points_h = []
        for i in range(n):
            tt = (hour - n + 1 + i) if unit == "hour" else (hour // 24 - n + 1 + i)
            tx, rx = series_c.get(tt, (0, 0))
            points_h.append({"t": tt * step, "tx": tx, "rx": rx, "all": series_all.get(tt, 0)})
        out = {
            "window": window, "t": round(now, 1), "cc": cc, "eu": cc in EU, "home": cc == (self.settings().get("home_country") or "").upper(),
            "geo": self.geo.info(),
            "totals": {"tx": tx_total, "rx": rx_total, "bytes": total, "share": (total / everything) if everything else 0.0,
                       "devices": len(devs), "operators": n_operators, "cities": len(cities), "no_city": max(0, no_city),
                       "with_detail": detail_total},
            "series": {"step": "hour" if unit == "hour" else "day", "points": points_h},
            "points": points, "cities": cities, "operators": operators, "devices": devs, "targets": targets,
        }
        self._country_cache[key] = (now, out, dev_key)
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
