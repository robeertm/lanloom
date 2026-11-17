"""UniFi Network family (local API): read half (stage A) and the executor's write half for clients (stage B).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

Two local APIs of the same controller are read by the web app, never written:

* the official integration API (``/proxy/network/integration/v1/...`` on a console, ``/integration/v1/...`` on a
  self-hosted application) with an API key - preferred wherever it covers an area;
* the classic API (``/proxy/network/api/s/<site>/...`` and ``/proxy/network/v2/api/site/<site>/...``) with a local
  view-only account - for what the official API does not have (rule hit counters, port profiles and port settings,
  port forwarding, fixed addresses, client ports) and as the only way on releases before 10.0.

At least one of the two credentials is needed; both together give the full picture.

The write half (``write.py``) runs only in the executor service, with a local account of its own that the app never
sees, and changes clients only: network (move), fixed address, name. See docs/integrations/unifi.md.
"""
from .. import IntegrationManifest

READ_AREAS = ["inventory", "clients", "topology", "networks", "zones", "policies", "acl", "ports", "port_profiles",
              "ssids", "wans", "vpn", "dhcp_reservations", "port_forwards", "stats", "firewall"]

CAPABILITIES = {
    "family": "unifi",
    "api": {"kind": "official-local", "version": ""},
    "read": list(READ_AREAS),
    "write": {"move": "B", "client.set.name": "B", "client.set.fixed_ip": "B", "client.set.network": "B"},
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10, "logins_per_hour": 4},
    "lockout": ["mgmt_network", "gateway_uplink", "self_path", "uplink_ports"],
}
# What the executor's write half can do (stage B, clients only); client settings exist only in the classic API.
WRITE_CAPABILITIES = dict(CAPABILITIES, api={"kind": "classic-local", "version": ""},
                          read=["inventory", "clients", "topology", "networks", "zones", "policies", "dhcp_reservations"])
# The read half itself never writes: what it publishes carries no write keys (the editor takes them from the executor).
READ_ONLY_WRITE_KEYS = ("move", "client.set")

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
    write="lanloom.integrations.unifi.write:UnifiWriter",
    docs="docs/integrations/unifi.md",
    config_fields=("url", "username", "site", "allow_self_signed"),
    secret_fields=("api_key", "password"),
    write_secret_fields=("write_password",),
    form=FORM,
    detect=({"port": 8443}, {"title": "unifi"}),   # identifier: default API port and web page title
)
