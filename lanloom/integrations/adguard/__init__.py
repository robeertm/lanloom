"""AdGuard Home family (local web API), read half only (stage A).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

The read half talks to the filter's local API (``/control/...``) with HTTP basic authentication. It asks for totals
only: the status, the statistics (per-hour counters, top lists, upstream response counts) and the filter lists. It
never asks for single queries (the allow-list holds no path of the query log) and sends no POST at all.
"""
from .. import IntegrationManifest

CAPABILITIES = {
    "family": "adguard",
    "api": {"kind": "local", "version": ""},
    "read": ["dns"],
    "write": {},                       # stage A: reading only
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10, "logins_per_hour": 12},
    "lockout": [],
}

FORM = (
    {"k": "url", "type": "url", "ph": "http://192.168.1.2:3000", "req": True},
    {"k": "username", "ph": "lanloom-read", "req": True},
    {"k": "password", "secret": True, "type": "password", "req": True},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="adguard",
    label="AdGuard Home (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.adguard.read:AdguardReader",
    docs="docs/integrations/adguard.md",
    config_fields=("url", "username", "allow_self_signed"),
    secret_fields=("password",),
    form=FORM,
    detect=({"port": 3000, "title": "adguard home"}, {"port": 80, "title": "adguard home"}, {"title": "adguard home"}),   # identifier: web page title
    catalog="dns_filter",
    prefill_scheme="http",
)
