"""MikroTik family (RouterOS REST API): read half only (stage A).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

The read half talks to the router's REST interface (``/rest``, served by the router's ``www-ssl`` or ``www`` service,
RouterOS 7.1 and later) with a user of its own that belongs to a group which may only read. Every request is a GET on
a fixed path (see ``read.ALLOW``); the router's REST interface also accepts changes and commands on the same paths
through other methods, and none of them can leave this process.

It reads the router's identity and resources, interfaces and ethernet ports (link, speed, power over Ethernet), the
bridge with its ports and VLAN table, addresses (the networks), the DHCP servers and leases, the address table, the
bridge host table, neighbours found by the discovery protocols, default routes with the DHCP and PPPoE clients (the
internet connections), the wireless registration tables, the firewall filter rules and the NAT rules, and turns them
into the neutral model (equipment, ports, end devices, networks, port forwards, internet lines, plain checks) plus the
topology observation the core merges with its other sources.
"""
from .. import IntegrationManifest

READ_AREAS = ["inventory", "clients", "topology", "networks", "ports", "policies", "port_forwards",
              "dhcp_reservations", "wans", "stats"]

CAPABILITIES = {
    "family": "mikrotik",
    "api": {"kind": "official-local", "version": ""},
    "read": list(READ_AREAS),
    "write": {},                       # stage A: reading only; the router has no safe mode over its API (see the guide)
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10},
    "lockout": ["mgmt_network", "gateway_uplink", "self_path", "uplink_ports", "admin_interface"],
}

# Plain settings and read-only credentials of the read half (the integration dialog builds its form from these).
FORM = (
    {"k": "url", "type": "url", "ph": "https://192.168.88.1", "req": True},
    {"k": "username", "ph": "lanloom", "req": True},
    {"k": "password", "secret": True, "type": "password", "req": True},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="mikrotik",
    label="MikroTik RouterOS (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.mikrotik.read:MikrotikReader",
    docs="docs/integrations/mikrotik.md",
    config_fields=("url", "username", "allow_self_signed"),
    secret_fields=("password",),
    form=FORM,
    report="lanloom.integrations.mikrotik.report:night_section",
    # identifiers: the router's web page title and its discovery announcements (the router answers on the web port
    # with a page called after the product, and announces itself over mDNS/SSDP with the product name)
    detect=({"port": 80, "title": "routeros"}, {"port": 443, "title": "routeros"}, {"title": "mikrotik"},
            {"ssdp": "mikrotik"}),
    guide="rest",
)
