"""UniFi Network family (local API), read half only (stage A).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

Two local APIs of the same controller are read, never written:

* the official integration API (``/proxy/network/integration/v1/...`` on a console, ``/integration/v1/...`` on a
  self-hosted application) with an API key - preferred wherever it covers an area;
* the classic API (``/proxy/network/api/s/<site>/...`` and ``/proxy/network/v2/api/site/<site>/...``) with a local
  view-only account - for what the official API does not have (rule hit counters, port profiles and port settings,
  port forwarding, fixed addresses, client ports) and as the only way on releases before 10.0.

At least one of the two credentials is needed; both together give the full picture. See
docs/integrations/unifi.md.
"""
from .. import IntegrationManifest

READ_AREAS = ["inventory", "clients", "topology", "networks", "zones", "policies", "acl", "ports", "port_profiles",
              "ssids", "wans", "vpn", "dhcp_reservations", "port_forwards", "stats", "firewall"]

CAPABILITIES = {
    "family": "unifi",
    "api": {"kind": "official-local", "version": ""},
    "read": list(READ_AREAS),
    "write": {},                       # stage A: reading only; writes come with the executor half
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10, "logins_per_hour": 4},
    "lockout": ["mgmt_network", "gateway_uplink", "self_path", "uplink_ports"],
}

# Plain settings and read-only credentials of the read half (the integration dialog builds its form from these).
FORM = (
    {"k": "url", "type": "url", "ph": "https://192.168.1.1", "req": True},
    {"k": "api_key", "secret": True, "type": "password"},
    {"k": "username"},
    {"k": "password", "secret": True, "type": "password"},
    {"k": "site", "ph": "default"},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="unifi",
    label="UniFi Network (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.unifi.read:UnifiReader",
    docs="docs/integrations/unifi.md",
    config_fields=("url", "username", "site", "allow_self_signed"),
    secret_fields=("api_key", "password"),
    form=FORM,
    detect=({"port": 8443}, {"title": "unifi"}),   # identifier: default API port and web page title
)
