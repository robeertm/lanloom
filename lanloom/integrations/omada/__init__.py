"""Omada Network family (controller, Open API): read half only (stage A).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

The read half talks to the controller's Open API (``https://<controller>:8043`` for a self-hosted controller, the
address of the hardware controller otherwise) with an *application* made for Lanloom (client ID and client secret,
client-credentials mode). The application's role decides what the controller lets it do; the guide asks for a role
that only views. The read half exchanges the pair for a short-lived access token and sends it with every read; the
exchange is the only POST it can make (see ``read.ALLOW``), everything else is a GET on the paths of ``read.py``.

It reads the site's devices (gateway, switches, access points) with their uplinks, the clients, LAN networks and
VLANs, switch ports and port profiles, wireless networks, access control rules (shown, never written) and the
gateway's internet connections, and turns them into the neutral model (equipment, ports, end devices, networks,
SSIDs, plain checks) plus the topology observation the core merges with its other sources.
"""
from .. import IntegrationManifest

READ_AREAS = ["inventory", "clients", "topology", "networks", "acl", "ports", "port_profiles", "ssids", "wans",
              "dhcp_reservations", "stats"]

CAPABILITIES = {
    "family": "omada",
    "api": {"kind": "official-local", "version": ""},
    "read": list(READ_AREAS),
    "write": {},                       # stage A: reading only; writing comes after a test bench of its own
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10, "logins_per_hour": 6},
    "lockout": ["mgmt_network", "gateway_uplink", "self_path", "uplink_ports"],
}

# Plain settings and read-only credentials of the read half (the integration dialog builds its form from these).
FORM = (
    {"k": "url", "type": "url", "ph": "https://192.168.1.10:8043", "req": True},
    {"k": "controller_id", "ph": "(found automatically)"},
    {"k": "client_id", "ph": "lanloom-read", "req": True},
    {"k": "client_secret", "secret": True, "type": "password", "req": True},
    {"k": "site", "ph": "Default"},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="omada",
    label="Omada Network (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.omada.read:OmadaReader",
    docs="docs/integrations/omada.md",
    config_fields=("url", "controller_id", "client_id", "site", "allow_self_signed"),
    secret_fields=("client_secret",),
    form=FORM,
    report="lanloom.integrations.omada.report:night_section",
    detect=({"port": 8043, "title": "omada"}, {"title": "omada"}),   # identifier: default API port and web page title
)
