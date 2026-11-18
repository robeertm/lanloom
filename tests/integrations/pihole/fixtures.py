"""Recorded-style answers of the filter's local API (documentation test networks only: 192.0.2.0/24, example names)."""
import time

NOW = 1_700_000_000.0               # fixed clock of the model tests


def pihole_history(now=NOW, hours=24, per_slot=(10, 3)):
    """10-minute slots over the last ``hours`` hours (6 per hour)."""
    base = (int(now) // 600) * 600
    return {"history": [{"timestamp": base - 600 * i, "total": per_slot[0], "cached": 2, "blocked": per_slot[1], "forwarded": 5}
                        for i in range(hours * 6 - 1, -1, -1)]}


DATA = {
    "summary": {"queries": {"total": 1440, "blocked": 432, "percent_blocked": 30.0, "unique_domains": 311, "forwarded": 700,
                            "cached": 308, "replies": {"SERVFAIL": 4, "IP": 900}},
                "clients": {"active": 4, "total": 6},
                "gravity": {"domains_being_blocked": 91234, "last_update": 1_699_900_000}},
    "history": pihole_history(),
    "top_blocked": {"domains": [{"domain": "tracker.example", "count": 120}, {"domain": "ads.example.net", "count": 80}]},
    "clients": {"clients": [{"name": "laptop.lan", "ip": "192.0.2.10", "count": 900}, {"name": "", "ip": "192.0.2.11", "count": 400}]},
    "clients_blocked": {"clients": [{"name": "laptop.lan", "ip": "192.0.2.10", "count": 300}]},
    "upstreams": {"upstreams": [{"ip": "cache", "name": "cache", "port": -1, "count": 308, "statistics": {"response": 0.0, "variance": 0.0}},
                                {"ip": "blocklist", "name": "blocklist", "port": -1, "count": 432, "statistics": {"response": 0.0, "variance": 0.0}},
                                {"ip": "192.0.2.53", "name": "resolver.lan", "port": 53, "count": 700,
                                 "statistics": {"response": 0.0123, "variance": 0.0001}}]},
    "blocking": {"blocking": "enabled", "timer": None},
    "lists": {"lists": [{"address": "https://lists.example.org/path/hosts.txt", "type": "block", "enabled": True, "number": 90000,
                         "date_updated": 1_699_900_000, "id": 1, "comment": None},
                        {"address": "https://allow.example.org/list.txt", "type": "allow", "enabled": True, "number": 5,
                         "date_updated": 1_699_900_000, "id": 2}]},
    "version": {"version": {"core": {"local": {"version": "v6.1", "branch": "master", "hash": "abc"}}, "web": {}, "ftl": {}}},
}
