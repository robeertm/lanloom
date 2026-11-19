"""OpenWrt family (local ubus API): read half (stage A) and the executor's write half for firewall rules (stage B).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

The read half talks to the router's ubus JSON-RPC endpoint (``https://<router>/ubus``, the same one the web interface
uses) with a login of its own whose access list only allows reading (see docs/integrations/openwrt.md). Every call is
one JSON-RPC document; the transport lets a call through only when its object, method and arguments are on the read
list - sign-in, board and system information, ``uci get`` of the network, firewall, dhcp and wireless
configuration, the live state of interfaces and devices, the host hints and address leases of the web interface's
helper, and the wireless state (``iwinfo``). Options whose name says they hold a secret (wireless keys, dial-up
passwords, tunnel keys) are dropped on arrival.

It turns the configuration into the neutral model: every firewall zone is a zone, the router itself is the gateway
zone, a rule from zone A to zone B is a policy, the zone forwardings and input policies follow the rules, port
forwards, fixed addresses, wireless networks, clients, WAN links, plain checks and a night report section.

The write half (``write.py``) runs only in the executor service, with a login of its own that the app never sees,
and changes firewall rules only: create a rule (a named section ``lanloom_<id>``, marked as Lanloom's own), change or
switch a rule on and off, delete a rule Lanloom created. On top of Lanloom's backup it uses the router's own way back:
``uci apply`` with ``rollback`` and a 90 second timeout puts the change live, and the router undoes it by itself
unless the executor sends ``uci confirm`` after its first good check (see docs/integrations/openwrt.md).
"""
from .. import IntegrationManifest

READ_AREAS = ["inventory", "clients", "networks", "zones", "policies", "ssids", "wans", "dhcp_reservations",
              "port_forwards", "firewall"]

CAPABILITIES = {
    "family": "openwrt",
    "api": {"kind": "official-local", "version": ""},
    "read": list(READ_AREAS),
    "write": {"policy.create": "B", "policy.update": "B", "policy.delete": "B"},
    "rollback": {"device_native": True, "object_snapshot": True},
    "limits": {"objects_per_plan": 10, "logins_per_hour": 4},
    "lockout": ["mgmt_network", "gateway_uplink", "self_path", "admin_interface"],
}
# What the executor's write half can do (stage B: firewall rules). Interfaces, zones, forwardings, redirects,
# wireless, address service and system settings are no write key of this family (wireless networks come later).
WRITE_CAPABILITIES = dict(CAPABILITIES, read=["zones", "networks", "policies"])
# The read half itself never writes: what it publishes carries no write keys (the editor takes them from the executor).
READ_CAPABILITIES = dict(CAPABILITIES, write={}, rollback={"device_native": False, "object_snapshot": True})

# Plain settings and read-only credentials of the read half (the integration dialog builds its form from these).
FORM = (
    {"k": "url", "type": "url", "ph": "https://192.168.1.1", "req": True},
    {"k": "username", "ph": "lanloom", "req": True},
    {"k": "password", "secret": True, "type": "password", "req": True},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="openwrt",
    label="OpenWrt (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.openwrt.read:OpenwrtReader",
    write="lanloom.integrations.openwrt.write:OpenwrtWriter",
    docs="docs/integrations/openwrt.md",
    config_fields=("url", "username", "allow_self_signed"),
    secret_fields=("password",),
    write_secret_fields=("write_password",),
    form=FORM,
    report="lanloom.integrations.openwrt.report:night_section",
    # the web interface's page title ends in "- LuCI" (identifier of its web interface)
    detect=({"port": 443, "title": "luci"}, {"port": 80, "title": "luci"}),
    guide="ubus",
)
