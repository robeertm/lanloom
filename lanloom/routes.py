"""Network routes for the data paths view (DW2): which networks and places the data passes on the way to a destination.

This module is pure functions only: no network access, no files, standard library only. The measurement itself runs in a
low-priority worker thread (``routeprobe.py``); ``routewatch.py`` schedules it and stores the condensed result.
Here the output of the probes is read and condensed into a chain of sections.

One hop list for every parser:
  [{"ttl": int, "ip": str|None, "rtt": float|None (smallest round-trip time in ms), "name": str|None (reverse DNS)}]
  sorted by ttl; a hop without an answer has ip=None. At most MAX_HOPS hops. Broken or empty input gives [] (never an
  exception). Names: at most NAME_MAX characters of [A-Za-z0-9.-] (otherwise None) - reverse DNS names come from foreign
  hands and are never trusted.

Where is a hop (honest, never more precise than known): exchange point (prefix list or exchange name in the router name)
> place code in the router name (airport or city code) > city data of the data paths option (approximate only).
Every place must fit the round-trip time: light in glass fibre covers about 200 km per ms, so a one-way distance is at most
RTT/2 x 200 km (plus a tolerance of 150 km). A place that is physically impossible in the measured time is dropped.

Sections of a chain: home (private addresses before the first public hop) -> provider (first public network) -> exchange
(exchange point) / transit (networks in between) -> network (the destination's network) -> target (the destination itself,
only country, operator and city - never its address). Consecutive hops of the same network are merged.

What is stored (``condense``): per section only kind, AS number, operator name, country, a place (city name and its centre
coordinates, where it came from and whether it is sure) and a round-trip time band. Never a router address, never a host
name, never the destination's address.
"""
import ipaddress
import json
import math
import re
import unicodedata

MAX_HOPS = 40
NAME_MAX = 120
KM_PER_MS = 200.0           # light in glass fibre; one-way distance per ms of round-trip time: RTT/2 x 200 km
TOLERANCE_KM = 150.0
KINDS = ("home", "provider", "exchange", "transit", "network", "target")
SOURCES = ("start", "exchange", "name", "city", "country")

_NAME_OK = re.compile(r"^[A-Za-z0-9.-]+$")
_IP_CHARS = re.compile(r"^[0-9A-Fa-f:.]+$")

# Addresses that never belong to the public internet: the home or the inside of the provider (documentation ranges
# deliberately do NOT count here, the tests use them as public addresses)
_HOME_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8", "0.0.0.0/8",
    "fc00::/7", "fe80::/10", "::1/128")]


# ---------------------------------------------------------------------------------------------------------- building blocks
def clean_ip(text):
    """Checked, normalised address as text - otherwise None."""
    if not isinstance(text, str):
        return None
    text = text.strip().strip("[]")
    if not text or len(text) > 45 or not _IP_CHARS.match(text):
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def clean_name(text):
    if not isinstance(text, str):
        return None
    text = text.strip().rstrip(".")[:NAME_MAX]
    if not text or not _NAME_OK.match(text) or clean_ip(text):
        return None
    return text


def _num(value):
    try:
        z = float(value)
    except (TypeError, ValueError):
        return None
    return z if math.isfinite(z) and 0 <= z < 100000 else None


def _ttl(value):
    try:
        t = int(float(value))
    except (TypeError, ValueError):
        return None
    return t if 1 <= t <= 255 else None


def is_home_ip(ip):
    """Private, carrier-grade NAT, link-local, unique local or loopback address (inside the home or the provider)."""
    a = clean_ip(ip)
    if not a:
        return False
    addr = ipaddress.ip_address(a)
    return any(addr.version == n.version and addr in n for n in _HOME_NETS)


def rtt_band(ms):
    """Round-trip time -> the band that is stored: 1 ms steps below 10 ms, 5 ms below 100 ms, 10 ms below 300 ms, 50 ms
    above (at least 1). None stays None."""
    v = _num(ms)
    if v is None:
        return None
    if v < 10:
        return max(1, int(round(v)))
    if v < 100:
        return int(5 * round(v / 5))
    if v < 300:
        return int(10 * round(v / 10))
    return int(50 * round(v / 50))


class _Hops:
    """Collects answers per ttl (smallest RTT, first address) and returns the one hop list."""

    def __init__(self):
        self.h = {}

    def add(self, ttl, ip=None, rtt=None, name=None, reached=False):
        if ttl is None:
            return
        e = self.h.setdefault(ttl, {"ttl": ttl, "ip": None, "rtt": None, "name": None})
        if reached and ip:
            e["reached"] = True
        if e["ip"] is None and ip:
            e["ip"] = ip
            e["name"] = name
        elif ip and ip == e["ip"] and e["name"] is None:
            e["name"] = name
        if rtt is not None and ip and ip == e["ip"] and (e["rtt"] is None or rtt < e["rtt"]):
            e["rtt"] = rtt

    def list(self, gaps=False):
        if not self.h:
            return []
        ttls = sorted(self.h)
        if gaps:
            ttls = list(range(1, max(self.h) + 1))
        out = []
        for t in ttls[:MAX_HOPS]:
            e = self.h.get(t) or {"ttl": t, "ip": None, "rtt": None, "name": None}
            hop = {"ttl": t, "ip": e["ip"], "rtt": round(e["rtt"], 3) if e["rtt"] is not None else None, "name": e["name"]}
            if e.get("reached"):
                hop["reached"] = True
                out.append(hop)
                break                                # nothing after the destination counts
            out.append(hop)
        return out


# ---------------------------------------------------------------------------------------------------------- parsers
def _json_objects(text):
    """Every JSON object of an output: line by line, otherwise the whole text at once."""
    if not isinstance(text, str) or not text.strip() or len(text) > 2_000_000:
        return
    whole = None
    try:
        whole = json.loads(text)
    except ValueError:
        pass
    if isinstance(whole, dict):
        yield whole
        return
    if isinstance(whole, list):
        for o in whole:
            if isinstance(o, dict):
                yield o
        return
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict):
                yield o


def read_scamper(text):
    """Recorded Paris traceroute output in the JSON form of the common measurement tools (``"type": "trace"`` objects; for
    imported or recorded measurements - Lanloom itself never starts a program) -> hop list (gaps without an answer have ip=None). Only the first
    trace object counts."""
    try:
        for o in _json_objects(text):
            if o.get("type") != "trace":
                continue
            s = _Hops()
            dst = clean_ip(o.get("dst"))
            for h in o.get("hops") or []:
                if not isinstance(h, dict):
                    continue
                ip = clean_ip(h.get("addr"))
                fam6 = bool(ip and ":" in ip)
                unreach = h.get("icmp_type") == (1 if fam6 else 3)
                s.add(_ttl(h.get("probe_ttl")), ip, _num(h.get("rtt")), clean_name(h.get("name")) if h.get("name") else None,
                      reached=bool(ip and dst and ip == dst and (unreach or o.get("stop_reason") == "COMPLETED")))
            return s.list(gaps=True)
    except Exception:       # never a crash because of foreign output
        return []
    return []


_TR_HEAD = re.compile(r"^\s*(\d{1,3})\s+(.*)$")
_TR_IP_PAREN = re.compile(r"^\(([0-9A-Fa-f:.]+)\)$")
_TR_RTT = re.compile(r"^(\d+(?:\.\d+)?)$")


def read_traceroute(text):
    """Classic ``traceroute -n -q 2 -w 2 -m 30 ...`` output, or without -n ("name (ip)") -> hop list. ``*`` = no answer;
    with several addresses in one line the first counts; marks like !H/!N/!X are ignored."""
    if not isinstance(text, str) or len(text) > 2_000_000:
        return []
    try:
        s = _Hops()
        for line in text.splitlines():
            m = _TR_HEAD.match(line)
            if not m:
                continue
            ttl = _ttl(m.group(1))
            if ttl is None:
                continue
            parts = m.group(2).split()
            first_ip = first_name = current = None
            times = []
            i = 0
            while i < len(parts):
                p = parts[i]
                if p == "*" or p.startswith("!") or p == "ms":
                    pass
                elif _TR_RTT.match(p) and i + 1 < len(parts) and parts[i + 1] == "ms":
                    z = _num(p)
                    if z is not None and current is not None and current == first_ip:
                        times.append(z)
                elif i + 1 < len(parts) and _TR_IP_PAREN.match(parts[i + 1]):
                    ip = clean_ip(parts[i + 1][1:-1])
                    current = ip
                    if ip and first_ip is None:
                        first_ip, first_name = ip, clean_name(p)
                    i += 1
                else:
                    ip = clean_ip(p)
                    if ip:
                        current = ip
                        if first_ip is None:
                            first_ip = ip
                i += 1
            if first_ip is None:
                s.add(ttl)
                continue
            s.add(ttl, first_ip, min(times) if times else None, first_name)
        return s.list()
    except Exception:
        return []


def read_probe(text):
    """Output of the own probe (``routeprobe.py``): one JSON object {"hops": [{"ttl", "ip", "rtt", "name"}], ...}."""
    try:
        for o in _json_objects(text):
            hops = o.get("hops")
            if not isinstance(hops, list):
                continue
            s = _Hops()
            for h in hops[:255]:
                if isinstance(h, dict):
                    ip = clean_ip(h.get("ip"))
                    s.add(_ttl(h.get("ttl")), ip, _num(h.get("rtt")), clean_name(h.get("name")) if ip else None, reached=h.get("reached") is True)
            return s.list(gaps=True)
    except Exception:
        return []
    return []


# ---------------------------------------------------------------------------------------------------------- places
# Router names mix everything; these tokens are never a place (interfaces, services, endings, chance hits).
_NEVER = frozenset("""net com org edu gov info biz int arpa uk de nl fr us eu io co gig ten tun sat bb core edge gw cr ar pe ce ix
ip mpls cust static dyn pool host dsl adsl cable fiber fibre ppp bras wan lan vpn dns lo ae be po vl xe et ge fa hu tel ntt
example invalid test local localhost router rtr border peer peering transit agg access dist srv server""".split())


def _slug(text):
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", t)


def places_from_name(name, table):
    """Reverse DNS name -> possible places, most likely first: [{"code", "name", "cc", "lat", "lon"}].
    Tokens: the name in lower case, split at . - _ and digits; also two neighbouring tokens joined ("new-york" -> newyork).
    Only tokens of the table; 3-letter codes only as a token of their own (never part of a word); longer hits first
    (5-letter codes and city names before 3-letter codes). The ending (TLD) never counts; hits only from the domain
    (second level) count only when the rest of the name gives nothing."""
    if not isinstance(name, str) or not isinstance(table, dict) or not name.strip():
        return []
    labels = name.strip().lower().rstrip(".")[:NAME_MAX * 2].split(".")
    if len(labels) > 1:
        labels = labels[:-1]                        # TLD
    hits = [[], []]                                 # [0] = device part, [1] = domain
    for idx, label in enumerate(labels):
        prio = 1 if (len(labels) >= 2 and idx == len(labels) - 1) else 0
        toks = [t for t in re.split(r"[^a-z]+", label) if t]
        cands = list(toks) + [a + b for a, b in zip(toks, toks[1:])]
        for pos, t in enumerate(cands):
            if t in _NEVER or len(t) < 3:
                continue
            e = table.get(t)
            if isinstance(e, dict) and isinstance(e.get("lat"), (int, float)) and isinstance(e.get("lon"), (int, float)):
                hits[prio].append((-len(t), idx, pos, t, e))
    pick = hits[0] or hits[1]
    pick.sort(key=lambda x: x[:3])
    out, seen = [], set()
    for _, _, _, code, e in pick:
        ko = (round(e["lat"], 2), round(e["lon"], 2))
        if ko in seen:
            continue
        seen.add(ko)
        out.append({"code": code, "name": e.get("name"), "cc": e.get("cc"), "lat": e["lat"], "lon": e["lon"]})
    return out


def distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance (haversine) in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def plausible(origin, place, rtt_ms):
    """Does the distance fit the round-trip time? one way <= rtt/2 x 200 km + 150 km. Without RTT or place: yes."""
    if rtt_ms is None or not origin or not place:
        return True
    try:
        d = distance_km(float(origin["lat"]), float(origin["lon"]), float(place["lat"]), float(place["lon"]))
        return d <= max(float(rtt_ms), 0.0) / 2.0 * KM_PER_MS + TOLERANCE_KM
    except (KeyError, TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------------------------------------- exchange points
# Exchange names in router names -> (name, city, country). Only whole tokens (split at . - _ and digits) or two
# neighbouring tokens joined. Names of exchange points are what the view shows; no other list is bundled.
EXCHANGE_NAMES = {
    "decix": ("DE-CIX", "Frankfurt", "DE"), "amsix": ("AMS-IX", "Amsterdam", "NL"), "linx": ("LINX", "London", "GB"),
    "franceix": ("France-IX", "Paris", "FR"), "nlix": ("NL-ix", "Amsterdam", "NL"), "bcix": ("BCIX", "Berlin", "DE"),
    "ecix": ("ECIX", "Berlin", "DE"), "netnod": ("Netnod", "Stockholm", "SE"), "mixit": ("MIX-IT", "Milan", "IT"),
    "espanix": ("ESPANIX", "Madrid", "ES"), "swissix": ("SwissIX", "Zurich", "CH"), "vix": ("VIX", "Vienna", "AT"),
    "six": ("SIX", "Seattle", "US"), "any2": ("Any2", "Los Angeles", "US"), "hkix": ("HKIX", "Hong Kong", "HK"),
    "jpix": ("JPIX", "Tokyo", "JP"), "sgix": ("SGIX", "Singapore", "SG"), "torix": ("TorIX", "Toronto", "CA"),
    "napafrica": ("NAPAfrica", "Johannesburg", "ZA"), "ixbr": ("IX.br", "Sao Paulo", "BR"),
}
_SHORT = {"vix", "six", "any2", "ixbr"}          # short names: only as a token of their own, never joined from two


def _city_place(city, cc, table):
    """City name of an exchange -> place from the table (same country), or None."""
    if not city or not isinstance(table, dict):
        return None
    e = table.get(_slug(city))
    if isinstance(e, dict) and "lat" in e and "lon" in e and (not cc or not e.get("cc") or e["cc"] == cc):
        return e
    return None


def _free_text(text):
    """Free display text (exchange and city names from a list): no control characters, at most 80 characters."""
    if not isinstance(text, str):
        return None
    t = "".join(c for c in text if c.isprintable())[:80].strip()
    return t or None


def _name_tokens(name):
    labels = str(name or "").lower().rstrip(".").split(".")
    if len(labels) > 1:
        labels = labels[:-1]
    out = []
    for label in labels:
        toks = []
        for x in [x for x in re.split(r"[^a-z0-9]+", label) if x]:
            m = re.match(r"^([a-z]+)(\d?)$", x)              # "any2" stays, "ae1" -> "ae"
            toks.append(x if x in EXCHANGE_NAMES else (m.group(1) if m else re.sub(r"\d+", "", x)))
        out.append([t for t in toks if t])
    return out


def exchange_from_name(name, table=None):
    """Router name -> {"name", "city", "cc"[, "lat", "lon"]} (exchange name in the router name) or None."""
    if not name:
        return None
    for toks in _name_tokens(name):
        cands = [(t, True) for t in toks] + [(a + b, False) for a, b in zip(toks, toks[1:])]
        for t, single in cands:
            if t in EXCHANGE_NAMES and (single or t not in _SHORT):
                n, city, cc = EXCHANGE_NAMES[t]
                info = {"name": n, "city": city, "cc": cc}
                ko = _city_place(city, cc, table)
                if ko:
                    info["lat"], info["lon"] = ko["lat"], ko["lon"]
                return info
    return None


class ExchangeTable:
    """Prefix -> exchange point. One dict per prefix length (network address as a number); a lookup tries only the
    lengths that exist."""

    def __init__(self):
        self.v = {4: {}, 6: {}}
        self.lengths = {4: [], 6: []}

    def add(self, net, info):
        self.v[net.version].setdefault(net.prefixlen, {}).setdefault(int(net.network_address), info)

    def done(self):
        for f in (4, 6):
            self.lengths[f] = sorted(self.v[f], reverse=True)
        return self

    def __len__(self):
        return sum(len(x) for d in self.v.values() for x in d.values())

    def find(self, ip):
        a = clean_ip(ip)
        if not a:
            return None
        addr = ipaddress.ip_address(a)
        bits = 32 if addr.version == 4 else 128
        n = int(addr)
        for pl in self.lengths[addr.version]:
            info = self.v[addr.version][pl].get((n >> (bits - pl)) << (bits - pl))
            if info:
                return dict(info)
        return None


def exchange_table(rows, places=None):
    """Compact rows [[prefix, exchange name, city, country], ...] -> ExchangeTable (with coordinates where the city is in
    ``places``). Invalid rows are skipped."""
    t = ExchangeTable()
    cache = {}
    for row in rows or ():
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            continue
        pre, name, city, cc = row
        try:
            net = ipaddress.ip_network(str(pre).strip(), strict=False)
        except ValueError:
            continue
        name, city = _free_text(name), _free_text(city)
        cc = str(cc or "")[:2].upper() or None
        if not name:
            continue
        info = {"name": name, "city": city, "cc": cc}
        key = (city, cc)
        if key not in cache:
            cache[key] = _city_place(city, cc, places) or False
        if cache[key]:
            info["lat"], info["lon"] = cache[key]["lat"], cache[key]["lon"]
        t.add(net, info)
    return t.done()


def compact_peeringdb(ixpfx, ix, ixlan=None):
    """PeeringDB lists (``/api/ixpfx``, ``/api/ix``, optional ``/api/ixlan``; each a list or {"data": [...]}) -> compact
    rows [[prefix, exchange name, city, country], ...], without prefixes that are also routed in the normal internet
    (in_dfz)."""
    def data(x):
        if isinstance(x, dict):
            x = x.get("data")
        return [e for e in x if isinstance(e, dict)] if isinstance(x, list) else []
    exes = {e["id"]: e for e in data(ix) if isinstance(e.get("id"), int)}
    lans = {e["id"]: e["ix_id"] for e in (data(ixlan) if ixlan is not None else [])
            if isinstance(e.get("id"), int) and isinstance(e.get("ix_id"), int)}
    out = []
    for p in data(ixpfx):
        if p.get("in_dfz") is True:
            continue
        pre = str(p.get("prefix", "")).strip()
        lid = p.get("ixlan_id")
        e = exes.get(lans.get(lid, lid))           # without the ixlan list: ixlan_id == ix_id (the usual case)
        if not e or not pre or len(pre) > 50:
            continue
        try:
            ipaddress.ip_network(pre, strict=False)
        except ValueError:
            continue
        out.append([pre, _free_text(e.get("name")), _free_text(e.get("city")), str(e.get("country") or "")[:2].upper() or None])
    return out


# ---------------------------------------------------------------------------------------------------------- locating a hop
def _place(lat, lon, name, cc, source, sure):
    return {"lat": round(float(lat), 4), "lon": round(float(lon), 4), "name": name, "cc": cc, "source": source, "sure": sure}


def locate(hop, city_place, exchange, name_places, start, start_rtt=0):
    """Where is this hop? ``hop`` = {ip, rtt, ...}; ``city_place`` = {name, lat, lon[, cc]} from the city data or None;
    ``exchange`` = hit of the exchange table or of the router name (with lat/lon) or None; ``name_places`` = result of
    ``places_from_name``; ``start`` = {lat, lon} of the home; ``start_rtt`` is taken off the hop's RTT.
    Order: exchange (sure) > router name (sure) > city data (approximate, sure=False) > None. Places that do not fit the
    RTT are dropped."""
    rtt = hop.get("rtt") if isinstance(hop, dict) else None
    if rtt is not None:
        rtt = max(rtt - (start_rtt or 0), 0.0)

    def ok(o):
        return plausible(start, o, rtt)

    if isinstance(exchange, dict) and isinstance(exchange.get("lat"), (int, float)) and isinstance(exchange.get("lon"), (int, float)) and ok(exchange):
        return _place(exchange["lat"], exchange["lon"], exchange.get("city") or exchange.get("name"), exchange.get("cc"), "exchange", True)
    for o in name_places or []:
        if isinstance(o, dict) and "lat" in o and "lon" in o and ok(o):
            return _place(o["lat"], o["lon"], o.get("name"), o.get("cc"), "name", True)
    if isinstance(city_place, dict) and isinstance(city_place.get("lat"), (int, float)) and isinstance(city_place.get("lon"), (int, float)) and ok(city_place):
        return _place(city_place["lat"], city_place["lon"], city_place.get("name"), city_place.get("cc"), "city", False)
    return None


# ---------------------------------------------------------------------------------------------------------- the chain
def _safe_geo(geo, ip):
    try:
        g = geo(ip) if geo else None
    except Exception:
        g = None
    return g if isinstance(g, dict) else {}


def build_chain(hops, geo, exchanges, places, start, target=None):
    """Hop list -> sections [{"kind", "asn", "operator", "cc", "place": {...}|None, "rtt", "hops": [{ttl, ip, name, rtt, place}]}].

    ``geo(ip)`` -> {"asn", "operator", "cc", "place": {name, lat, lon}|None} (the data paths' own lookup); ``exchanges`` =
    ExchangeTable or None; ``places`` = code -> place (route-places.json); ``start`` = {lat, lon[, name, cc]} of the home;
    ``target`` = {"cc", "asn", "operator", "city", "anycast"} of the destination (no address) - with it the chain ends in a
    section "target". The chain always starts with a section "home" (possibly without hops). Hops without an answer and
    hops without a known network stay with the running section. RTT of a section = that of its last answering hop."""
    if not isinstance(hops, list):
        return []
    start = start if isinstance(start, dict) else None
    home = {"kind": "home", "asn": None, "operator": None, "cc": (start or {}).get("cc"), "place": None, "rtt": None, "hops": []}
    if start and isinstance(start.get("lat"), (int, float)) and isinstance(start.get("lon"), (int, float)):
        home["place"] = _place(start["lat"], start["lon"], start.get("name"), start.get("cc"), "start", True)
    chain = [home]
    cur = home
    public = False
    provider = False
    key = None
    reached = False
    for h in hops[:MAX_HOPS]:
        if not isinstance(h, dict):
            continue
        ip = clean_ip(h.get("ip"))
        entry = {"ttl": h.get("ttl"), "ip": ip, "name": clean_name(h.get("name")), "rtt": h.get("rtt"), "place": None}
        if h.get("reached"):
            reached = True
        if not ip or is_home_ip(ip):
            cur["hops"].append(entry)
            if ip and h.get("rtt") is not None:
                cur["rtt"] = h["rtt"]
            continue
        info = exchanges.find(ip) if exchanges is not None else None
        if info is None and entry["name"]:
            info = exchange_from_name(entry["name"], places)
        g = _safe_geo(geo, ip)
        city = g.get("place") if isinstance(g.get("place"), dict) else None
        if city and g.get("cc") and "cc" not in city:
            city = dict(city, cc=g.get("cc"))
        by_name = places_from_name(entry["name"], places) if entry["name"] else []
        entry["place"] = locate(h, city, info, by_name, start)
        asn = g.get("asn") if isinstance(g.get("asn"), int) and g.get("asn") > 0 else None
        cc = (g.get("cc") or None) if isinstance(g.get("cc"), str) and len(g.get("cc") or "") == 2 else None
        if info:
            new = ("exchange", info.get("name"))
            kind, operator, asn_s, cc_s = "exchange", info.get("name"), None, info.get("cc")
        elif asn:
            new = ("as", asn)
            kind, operator, asn_s, cc_s = ("transit" if provider else "provider"), g.get("operator") or None, asn, cc
        elif not public:                              # first public hop without a known network: provider without a number
            new = ("as", None, cc)
            kind, operator, asn_s, cc_s = "provider", g.get("operator") or None, None, cc
        elif cc and cur["kind"] != "exchange" and cur.get("asn") is None and cur.get("cc") != cc:
            new = ("cc", cc)                         # without network numbers: one section per country
            kind, operator, asn_s, cc_s = "transit", None, None, cc
        else:
            cur["hops"].append(entry)                 # unknown network in the middle: stays with the running section
            _follow(cur, h, entry)
            continue
        if new != key or cur is home:
            if kind == "provider":
                provider = True
            cur = {"kind": kind, "asn": asn_s, "operator": operator, "cc": cc_s, "place": None, "rtt": None, "hops": []}
            chain.append(cur)
            key = new
        public = True
        cur["hops"].append(entry)
        _follow(cur, h, entry)
    nets = [k for k in chain if k["kind"] in ("provider", "transit")]
    if len(nets) > 1:
        nets[-1]["kind"] = "network"
    if isinstance(target, dict):
        last_rtt = next((k["rtt"] for k in reversed(chain) if k["rtt"] is not None), None)
        anycast = bool(target.get("anycast"))
        t = {"kind": "target", "asn": None if anycast else (target.get("asn") or None),
             "operator": None if anycast else (target.get("operator") or None),
             "cc": None if anycast else (target.get("cc") or None), "anycast": anycast,
             "city": None if anycast else (target.get("city") or None),
             "place": None, "rtt": last_rtt if reached else None, "reached": reached, "hops": []}
        tp = target.get("place")
        if not anycast and isinstance(tp, dict) and isinstance(tp.get("lat"), (int, float)) and isinstance(tp.get("lon"), (int, float)):
            rt = t["rtt"] if t["rtt"] is not None else last_rtt
            if plausible(start, tp, rt):
                t["place"] = _place(tp["lat"], tp["lon"], tp.get("name"), target.get("cc"), "city", False)
        chain.append(t)
    return chain


def _follow(section, hop, entry):
    if hop.get("rtt") is not None:
        section["rtt"] = hop["rtt"]
    if entry.get("place"):
        section["place"] = entry["place"]


def condense(chain):
    """Chain -> what is stored and shown: no router address, no host name, RTT as a band. Sections without anything
    known collapse into their neighbour; a section keeps its number of hops."""
    out = []
    for k in chain or ():
        if not isinstance(k, dict) or k.get("kind") not in KINDS:
            continue
        p = k.get("place")
        place = None
        if isinstance(p, dict) and isinstance(p.get("lat"), (int, float)) and isinstance(p.get("lon"), (int, float)):
            place = {"name": _free_text(p.get("name")), "cc": p.get("cc") if isinstance(p.get("cc"), str) and len(p["cc"]) == 2 else None,
                     "lat": round(float(p["lat"]), 2), "lon": round(float(p["lon"]), 2),
                     "source": p.get("source") if p.get("source") in SOURCES else "city", "sure": bool(p.get("sure"))}
        s = {"kind": k["kind"], "asn": k.get("asn") if isinstance(k.get("asn"), int) and k.get("asn") > 0 else None,
             "operator": _free_text(k.get("operator")),
             "cc": k.get("cc") if isinstance(k.get("cc"), str) and re.fullmatch(r"[A-Z]{2}", k.get("cc") or "") else None,
             "place": place, "rtt": rtt_band(k.get("rtt")), "hops": len([x for x in k.get("hops") or () if isinstance(x, dict)])}
        if k["kind"] == "target":
            s["anycast"] = bool(k.get("anycast"))
            s["reached"] = bool(k.get("reached"))
            s["city"] = _free_text(k.get("city"))
        out.append(s)
    return out


def signature(sections):
    """The part of a condensed chain that says "the route changed": the order of networks and exchange points (not the
    RTT, not the places - those move a little all the time)."""
    parts = []
    for s in sections or ():
        if not isinstance(s, dict) or s.get("kind") in ("home", "target"):
            continue
        ident = s.get("asn") or (s.get("operator") if s.get("kind") == "exchange" else None) or s.get("cc") or "?"
        parts.append("%s:%s" % (s.get("kind")[:1], ident))
    return ">".join(parts)[:400]


def is_estimated(section):
    """A section whose place is only approximate: a place from the city data, or only its country. The destination without
    a place of its own is not estimated (it is drawn where its network is); a section with nothing known is not drawn."""
    if not isinstance(section, dict) or section.get("kind") == "home":
        return False
    p = section.get("place")
    if isinstance(p, dict):
        return not p.get("sure")
    return bool(section.get("cc")) and section.get("kind") != "target"


def uncertain(sections):
    """True if any section of the chain is estimated (``is_estimated``)."""
    return any(is_estimated(s) for s in sections or ())
