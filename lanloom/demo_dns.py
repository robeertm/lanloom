"""Sample DNS filter for the demo: one reading per minute and 35 days of sums per hour. Everything is invented (documentation
addresses, example names). The filter sits on an address no sample device has, so it shows as a component of its own."""
import math
import random
import time

FILTER_URL = "http://192.168.50.18"
HOUR_S = 3600
# client address -> (weight in the traffic, blocked share): a TV and a vacuum talk to many trackers, a laptop few
CLIENTS = (("192.168.50.20", 0.22, 0.18, "office-laptop.lan"), ("192.168.50.21", 0.20, 0.22, "studio-desktop.lan"),
           ("192.168.50.22", 0.14, 0.36, "phone-1.lan"), ("192.168.50.30", 0.13, 0.62, "living-room-tv.lan"),
           ("192.168.50.23", 0.09, 0.33, "tablet.lan"), ("192.168.50.31", 0.06, 0.41, "kitchen-speaker.lan"),
           ("192.168.50.65", 0.04, 0.71, "vacuum.lan"), ("192.168.50.63", 0.03, 0.27, "light-strip.lan"))
BLOCKED = (("tracker.example", 0.24), ("metrics.example.net", 0.19), ("ads.example.org", 0.17), ("telemetry.example.com", 0.15),
           ("pixel.example.net", 0.11), ("beacon.example.org", 0.08), ("stats.example.com", 0.06))
LISTS = (("lists.example.org/ads-and-trackers", 87_412, 2), ("lists.example.org/telemetry", 14_903, 2), ("lists.example.net/malware", 31_250, 9))


def _rate(hour_number: int, rng: random.Random) -> int:
    """Queries in one hour: calm at night, busy in the evening."""
    h = hour_number % 24
    base = 35 + 150 * max(0.0, math.sin((h - 6) / 17 * math.pi)) ** 1.5
    return max(0, int(base * rng.uniform(0.85, 1.15)))


def history(now: float, days: int = 35, today_share: float = 0.47) -> list:
    """[[hour, queries, blocked], ...] for ``days`` days up to and including the current hour; the last 24 hours block more
    than usual (a new tracker-heavy device), so the card and the night report have something to say."""
    rng = random.Random(41)
    cur = int(now // HOUR_S)
    out = []
    for hn in range(cur - days * 24 + 1, cur + 1):
        q = _rate(hn, rng)
        share = rng.uniform(0.25, 0.31) if hn <= cur - 24 else rng.uniform(today_share - 0.03, today_share + 0.03)
        out.append([hn, q, int(q * share)])
    return out


def reading(now: float, family: str) -> dict:
    """One neutral reading (lanloom.dnsfilter): the last 24 hours as the filter would report them."""
    rows = history(now)[-24:]
    q, b = sum(r[1] for r in rows), sum(r[2] for r in rows)
    clients = [{"ip": ip, "name": name, "q": int(q * w), "b": int(q * w * s)} for ip, w, s, name in CLIENTS]
    return {"v": 1, "family": family, "version": "demo", "t": now, "running": True, "blocking": True, "window_s": 86400,
            "totals": {"queries": q, "blocked": b, "clients": len(CLIENTS) + 2, "list_entries": sum(n for _, n, _ in LISTS)},
            "hours": rows, "days": [],
            "top_blocked": [{"domain": d, "n": int(b * w)} for d, w in BLOCKED],
            "top_clients": clients,
            "upstreams": [{"name": "203.0.113.53:53", "n": int((q - b) * 0.6), "ms": 14.2},
                          {"name": "203.0.113.54:53", "n": int((q - b) * 0.3), "ms": 22.8}],
            "upstream_ok": True,
            "lists": [{"name": n, "enabled": True, "entries": e, "updated": int(now - d * 86400)} for n, e, d in LISTS],
            "lists_updated": int(now - 2 * 86400)}


def seed_history(core, integ_id: int, now: float = None) -> None:
    """35 days of sums per hour, once (an existing history is left alone)."""
    now = time.time() if now is None else now
    if core.dns_log.has_data(integ_id):
        return
    core.dns_log.record(integ_id, {"hours": history(now)}, now)
