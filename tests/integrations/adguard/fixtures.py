"""Recorded-style answers of the filter's local API (documentation test networks only: 192.0.2.0/24, example names)."""
import time

NOW = 1_700_000_000.0               # fixed clock of the model tests


DATA = {
    "status": {"dns_addresses": ["192.0.2.2"], "dns_port": 53, "http_port": 3000, "protection_enabled": True, "running": True,
               "version": "v0.107.50"},
    "stats": {"time_units": "hours", "num_dns_queries": 2400, "num_blocked_filtering": 600, "num_replaced_safebrowsing": 10,
              "num_replaced_safesearch": 3, "num_replaced_parental": 5, "avg_processing_time": 0.004,
              "top_queried_domains": [{"example.com": 90}], "top_clients": [{"192.0.2.10": 1500}, {"phone": 300}],
              "top_blocked_domains": [{"tracker.example": 400}, {"ads.example.net": 150}],
              "top_upstreams_responses": [{"192.0.2.53:53": 1500}], "top_upstreams_avg_time": [{"192.0.2.53:53": 0.021}],
              "dns_queries": [100] * 24, "blocked_filtering": [25] * 24, "replaced_safebrowsing": [0] * 23 + [1],
              "replaced_parental": [0] * 24, "replaced_safesearch": [0] * 24},
    "filtering": {"enabled": True, "interval": 24,
                  "filters": [{"id": 1, "enabled": True, "url": "https://lists.example.org/a.txt?token=SECRET", "name": "Main list",
                               "rules_count": 50000, "last_updated": "2023-11-14T10:00:00Z"},
                              {"id": 2, "enabled": False, "url": "https://lists.example.org/b.txt", "name": "Off list",
                               "rules_count": 10, "last_updated": "2023-01-01T00:00:00Z"}],
                  "user_rules": ["||secret-private-name.example^"]},
}
