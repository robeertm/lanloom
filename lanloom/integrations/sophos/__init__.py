"""Sophos Firewall family (local XML API), read half only (stage A).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

The read half talks to the firewall's local XML API (``https://<firewall>:4444/webconsole/APIController``) with a
dedicated administrator account whose profile only allows reading (see docs/integrations/sophos.md). The API has
no session and no token: every request is one query document that carries the account and a ``<Get>`` list. The
transport only lets a document through that holds exactly the account and a ``<Get>`` of the entities named in
``read.READ_ENTITIES`` - no ``<Set>``, no ``<Remove>``, nothing else.

It reads zones (with the services they allow on the firewall itself), interfaces and VLANs, address and service
objects, firewall rules and rule groups, NAT rules (port forwarding), DHCP servers with fixed addresses, static
routes and VPN connections, and turns them into the neutral model (zones, networks, policies, port forwarding,
WAN, VPN, plain checks).
"""
from .. import IntegrationManifest

READ_AREAS = ["zones", "networks", "policies", "port_forwards", "dhcp_reservations", "wans", "vpn", "firewall"]

CAPABILITIES = {
    "family": "sophos",
    "api": {"kind": "official-local", "version": ""},
    "read": list(READ_AREAS),
    "write": {},                       # stage A: reading only; writing comes after a test bench of its own
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10, "logins_per_hour": 12},
    "lockout": ["mgmt_network", "gateway_uplink", "self_path", "admin_interface"],
}

# Plain settings and read-only credentials of the read half (the integration dialog builds its form from these).
FORM = (
    {"k": "url", "type": "url", "ph": "https://192.168.1.1:4444", "req": True},
    {"k": "username", "ph": "lanloom-read", "req": True},
    {"k": "password", "secret": True, "type": "password", "req": True},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="sophos",
    label="Sophos Firewall (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.sophos.read:SophosReader",
    docs="docs/integrations/sophos.md",
    config_fields=("url", "username", "allow_self_signed"),
    secret_fields=("password",),
    form=FORM,
    report="lanloom.integrations.sophos.report:night_section",
    detect=({"port": 4444, "title": "sophos"},),   # identifier: default admin port and web page title
)
