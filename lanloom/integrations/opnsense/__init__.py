"""OPNsense family (local REST API): read half (stage A) and the executor's write half for firewall rules (stage B).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

The read half talks to the firewall's REST API (``https://<firewall>/api/...``) with an API key and secret of a user
made only for Lanloom (Basic authentication, no sign-in, no session; see docs/integrations/opnsense.md). It sends GET
requests only, to a fixed list of paths: interfaces, interface groups and VLANs, the firewall rules of the rule API,
aliases, source and destination NAT, address reservations, static routes, gateway status and the VPN instances. The
firewall's privileges are granted per page and do not separate reading from writing, so the guide asks for a user
with only the pages Lanloom reads - and the transport cannot send anything but those GET requests either way.

It turns the configuration into the neutral model: every assigned interface (and every interface group) is a zone,
the firewall itself is the gateway zone, a rule on an interface is a policy from that zone to the zone of its
destination, port forwarding, address reservations, WAN links, VPN instances, plain checks and a night report
section.

The write half (``write.py``) runs only in the executor service, with an API key of its own that the app never sees,
and changes firewall rules of the rule API only: create a rule (marked as Lanloom's own), change or switch a rule on
and off, delete a rule Lanloom created. Releases that have it get the firewall's own way back on top of Lanloom's
backup: a savepoint before the change and a 60 second timer that puts the rules back unless the executor confirms
after its first good check (see docs/integrations/opnsense.md, "Way back per release").
"""
from .. import IntegrationManifest

READ_AREAS = ["zones", "networks", "policies", "port_forwards", "dhcp_reservations", "wans", "vpn", "firewall"]

CAPABILITIES = {
    "family": "opnsense",
    "api": {"kind": "official-local", "version": ""},
    "read": list(READ_AREAS),
    "write": {"policy.create": "B", "policy.update": "B", "policy.delete": "B"},
    "rollback": {"device_native": True, "object_snapshot": True},
    "limits": {"objects_per_plan": 10},
    "lockout": ["mgmt_network", "gateway_uplink", "self_path", "admin_interface"],
}
# What the executor's write half can do (stage B: firewall rules of the rule API). Interfaces, VLANs, NAT, aliases,
# address service, VPN and system settings are no write key of this family. The device's own way back depends on the
# release (``model.native_rollback``); the write half narrows ``rollback.device_native`` after its version probe.
WRITE_CAPABILITIES = dict(CAPABILITIES, read=["zones", "networks", "policies"])
# The read half itself never writes: what it publishes carries no write keys (the editor takes them from the executor).
READ_CAPABILITIES = dict(CAPABILITIES, write={}, rollback={"device_native": False, "object_snapshot": True})

# Plain settings and read-only credentials of the read half (the integration dialog builds its form from these).
FORM = (
    {"k": "url", "type": "url", "ph": "https://192.168.1.1", "req": True},
    {"k": "api_key", "ph": "API key", "req": True},
    {"k": "api_secret", "secret": True, "type": "password", "req": True},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="opnsense",
    label="OPNsense (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.opnsense.read:OpnsenseReader",
    write="lanloom.integrations.opnsense.write:OpnsenseWriter",
    docs="docs/integrations/opnsense.md",
    config_fields=("url", "api_key", "allow_self_signed"),
    secret_fields=("api_secret",),
    write_secret_fields=("write_api_secret",),
    form=FORM,
    report="lanloom.integrations.opnsense.report:night_section",
    detect=({"port": 443, "title": "opnsense"},),     # identifier: web interface port and the login page title
    guide="apikey",
)
