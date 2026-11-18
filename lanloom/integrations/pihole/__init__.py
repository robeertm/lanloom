"""Pi-hole family (local web API, version 6 and later), read half only (stage A).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

The read half talks to the filter's local API (``/api/...``). It asks for totals only: the summary, the 10-minute
history, the top blocked names, the top clients, the upstream resolvers, the state of the blocking and the block lists.
It never asks for single queries (the allow-list holds no path of the query log). The only POST it may send is the
sign-in with the password; the session is reused, and a wrong password pauses the half for 15 minutes.
"""
from .. import IntegrationManifest

CAPABILITIES = {
    "family": "pihole",
    "api": {"kind": "local", "version": ""},
    "read": ["dns"],
    "write": {},                       # stage A: reading only
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10, "logins_per_hour": 12},
    "lockout": [],
}

FORM = (
    {"k": "url", "type": "url", "ph": "http://192.168.1.2", "req": True},
    {"k": "password", "secret": True, "type": "password", "ph": "app password"},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="pihole",
    label="Pi-hole (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.pihole.read:PiholeReader",
    docs="docs/integrations/pihole.md",
    config_fields=("url", "allow_self_signed"),
    secret_fields=("password",),
    form=FORM,
    detect=({"port": 80, "title": "pi-hole"}, {"port": 443, "title": "pi-hole"}, {"port": 8080, "title": "pi-hole"}),   # identifier: web page title
    catalog="dns_filter",
    prefill_scheme="http",
)
